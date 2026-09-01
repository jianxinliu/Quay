"""数据库用户与权限管理：目录查询 SQL + DCL 语句构造（纯函数，可单测）。

**为什么单独一层**：DCL（CREATE USER / GRANT / REVOKE）与普通 SQL 有两点不同——

1. **语句由页面表单拼出来，不是用户手写的**。名字、库名、表名会被拼进标识符位置，
   privileges 会被拼进关键字位置，一旦不做校验就是注入面。所以这里对每个成分都
   「先白名单/字符校验、再按方言引用」，两道防线；构造出的语句直接可执行，
   调用方不再拼字符串。
2. **CREATE USER / ALTER USER 的语句里带明文密码**。密码不能落审计、不能落日志
   （见 CLAUDE.md 安全红线 2），所以每个构造函数返回 `DclStatement`，同时给出
   `sql`（真正执行的）与 `audit_sql`（密码已换成 ***，用于审计与页面回显）。

目录查询（列用户 / 看授权 / 权限矩阵）刻意不用 `information_schema.*_privileges`：
那些视图只反映「与当前登录角色相关」的授权，管理员要看的是**任意账号**的全貌，
所以 PG 走 `pg_class.relacl` + `aclexplode`、MySQL 走 `mysql.user` 与
`information_schema.table_privileges`（后者在 MySQL 上是全局视图，无此限制）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SUPPORTED_ENGINES = ("postgres", "mysql")

# 标识符（角色名/库名/表名/schema 名）允许的字符。刻意收得比数据库本身严：
# 这些值来自页面表单，宽松没有收益，出问题却是注入。需要更奇怪的名字请用查询台手写 SQL。
_NAME_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_$.\-]*$")
# MySQL 的 host 部分可以带通配符 % 与 _，也可以是 IP/网段
_HOST_RE = re.compile(r"^[A-Za-z0-9_%.\-:]+$")
_MAX_NAME_LEN = {"postgres": 63, "mysql": 64}

# 每种引擎、每个授权层级允许的权限关键字。白名单，不在表里的一律拒——
# 权限词会被原样拼进 SQL 关键字位置，不能靠引用来防。
_PG_PRIVS = {
    "table": ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL"),
    "schema": ("USAGE", "CREATE", "ALL"),
    "database": ("CONNECT", "CREATE", "TEMPORARY", "ALL"),
    "sequence": ("USAGE", "SELECT", "UPDATE", "ALL"),
}
_MYSQL_PRIVS = {
    "table": ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "INDEX", "ALTER",
              "CREATE VIEW", "SHOW VIEW", "TRIGGER", "REFERENCES", "ALL PRIVILEGES"),
    "database": ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "INDEX", "ALTER",
                 "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EXECUTE",
                 "EVENT", "TRIGGER", "LOCK TABLES", "CREATE TEMPORARY TABLES",
                 "ALL PRIVILEGES"),
    "global": ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "RELOAD", "PROCESS",
               "SHOW DATABASES", "SUPER", "CREATE USER", "REPLICATION SLAVE",
               "REPLICATION CLIENT", "ALL PRIVILEGES"),
}

# PG 的 ALTER DEFAULT PRIVILEGES 作用的对象类型
_PG_DEFAULT_OBJ = ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES", "SCHEMAS")

REDACTED = "***"


class PrivilegeError(ValueError):
    """输入不合法（名字非法 / 权限不在白名单 / 引擎不支持）。调用方转成 4xx。"""


@dataclass(frozen=True)
class DclStatement:
    """一条待执行的 DCL。`sql` 是真正发给 DB 的，`audit_sql` 是脱敏后用于审计/回显的。

    两者仅在语句含明文密码时不同——其余场景 `audit_sql` 就等于 `sql`。
    """

    sql: str
    audit_sql: str
    summary: str  # 一句人话，用于确认卡片标题

    @property
    def has_secret(self) -> bool:
        return self.sql != self.audit_sql


# ---------------------------------------------------------------- 校验与引用

def check_engine(engine_kind: str) -> str:
    if engine_kind not in SUPPORTED_ENGINES:
        raise PrivilegeError(
            f"权限管理暂不支持 {engine_kind} 引擎（支持：{', '.join(SUPPORTED_ENGINES)}）")
    return engine_kind


def validate_name(name: str, engine_kind: str, what: str = "名称") -> str:
    name = (name or "").strip()
    if not name:
        raise PrivilegeError(f"{what}不能为空")
    if len(name) > _MAX_NAME_LEN.get(engine_kind, 64):
        raise PrivilegeError(f"{what}过长（上限 {_MAX_NAME_LEN.get(engine_kind, 64)} 字符）")
    if not _NAME_RE.match(name):
        raise PrivilegeError(
            f"{what} {name!r} 含不允许的字符——只接受字母、数字、下划线、$、点、连字符，"
            "且须以字母/数字/下划线/$ 开头。特殊名字请到查询台手写 SQL。")
    return name


def validate_host(host: str) -> str:
    """MySQL 账号的 host 部分。空串按 `%`（任意主机）处理，与 MySQL 习惯一致。"""
    host = (host or "").strip() or "%"
    if len(host) > 255 or not _HOST_RE.match(host):
        raise PrivilegeError(f"host {host!r} 不合法（只接受字母、数字、. - _ % : ）")
    return host


def quote_ident(name: str, engine_kind: str) -> str:
    """按方言引用标识符。名字已过 validate_name，这里的转义是第二道防线。"""
    if engine_kind == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str, engine_kind: str) -> str:
    """引用字符串字面量（密码、MySQL 的 user/host）。

    MySQL 默认开 backslash escape，反斜杠必须一并转义；PG 标准模式下只需重复单引号。
    """
    text = str(value)
    if engine_kind == "mysql":
        text = text.replace("\\", "\\\\")
    return "'" + text.replace("'", "''") + "'"


def mysql_account(name: str, host: str) -> str:
    """MySQL 的账号写法 `'user'@'host'`——注意这里是**字面量**不是标识符。"""
    return f"{quote_literal(name, 'mysql')}@{quote_literal(host, 'mysql')}"


def normalize_privileges(privileges: list[str], engine_kind: str, level: str) -> list[str]:
    """把页面传来的权限列表归一化为白名单里的大写关键字；有一个不认识就整体拒绝。"""
    table = _PG_PRIVS if engine_kind == "postgres" else _MYSQL_PRIVS
    allowed = table.get(level)
    if allowed is None:
        raise PrivilegeError(f"{engine_kind} 不支持 {level} 级授权")
    out: list[str] = []
    for raw in privileges or []:
        priv = re.sub(r"\s+", " ", str(raw).strip().upper())
        if priv not in allowed:
            raise PrivilegeError(
                f"权限 {raw!r} 不在 {engine_kind} 的 {level} 级白名单内"
                f"（允许：{', '.join(allowed)}）")
        if priv not in out:
            out.append(priv)
    if not out:
        raise PrivilegeError("请至少选择一项权限")
    # ALL 与具体权限并列时以 ALL 为准，避免拼出 `GRANT ALL, SELECT`
    for whole in ("ALL PRIVILEGES", "ALL"):
        if whole in out:
            return [whole]
    return out


# ---------------------------------------------------------------- 目录查询

_PG_LIST_USERS = """
SELECT r.rolname                                        AS name,
       r.rolcanlogin                                    AS can_login,
       r.rolsuper                                       AS is_superuser,
       r.rolcreatedb                                    AS can_create_db,
       r.rolcreaterole                                  AS can_create_role,
       r.rolreplication                                 AS replication,
       r.rolconnlimit                                   AS conn_limit,
       r.rolvaliduntil                                  AS valid_until,
       ARRAY(SELECT b.rolname FROM pg_auth_members m
              JOIN pg_roles b ON b.oid = m.roleid
             WHERE m.member = r.oid ORDER BY 1)         AS member_of
  FROM pg_roles r
 WHERE r.rolname NOT LIKE 'pg\\_%'
 ORDER BY r.rolcanlogin DESC, r.rolname
"""

# mysql.user 的列在各版本有出入，只取所有 5.7+/8.x 都有的几列
_MYSQL_LIST_USERS = """
SELECT u.user                                    AS name,
       u.host                                    AS host,
       u.plugin                                  AS plugin,
       u.account_locked                          AS locked,
       u.password_expired                        AS password_expired,
       u.max_user_connections                    AS conn_limit
  FROM mysql.user u
 ORDER BY u.user, u.host
"""


def list_users_sql(engine_kind: str) -> str:
    """列出数据库里的账号 / 角色。"""
    check_engine(engine_kind)
    return _PG_LIST_USERS if engine_kind == "postgres" else _MYSQL_LIST_USERS


def user_grants_sql(engine_kind: str, name: str, host: str = "%") -> list[tuple[str, str]]:
    """某账号的授权明细。返回 [(分组标题, SQL)]——一个账号的授权分散在多个层级。

    PG 用 `aclexplode` 展开对象上的 ACL，而不是 information_schema：
    后者只反映与当前登录角色相关的授权，管理员要看的是任意账号的全貌。
    MySQL 直接 `SHOW GRANTS FOR`，它本身就是权威且已汇总好的形式。
    """
    check_engine(engine_kind)
    if engine_kind == "mysql":
        validate_name(name, "mysql", "用户名")
        return [("授权", f"SHOW GRANTS FOR {mysql_account(name, validate_host(host))}")]
    validate_name(name, "postgres", "角色名")
    who = quote_literal(name, "postgres")
    return [
        ("表 / 视图", f"""
SELECT n.nspname AS "schema", c.relname AS "object",
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'matview' ELSE c.relkind::text END AS "kind",
       a.privilege_type AS "privilege", a.is_grantable AS "grantable"
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE c.relkind IN ('r','p','v','m')
   AND n.nspname NOT IN ('pg_catalog','information_schema')
   AND pg_get_userbyid(a.grantee) = {who}
 ORDER BY 1, 2, 4
"""),
        ("Schema", f"""
SELECT n.nspname AS "schema", a.privilege_type AS "privilege", a.is_grantable AS "grantable"
  FROM pg_namespace n
 CROSS JOIN LATERAL aclexplode(n.nspacl) a
 WHERE n.nspname NOT IN ('pg_catalog','information_schema')
   AND pg_get_userbyid(a.grantee) = {who}
 ORDER BY 1, 2
"""),
        ("数据库", f"""
SELECT d.datname AS "database", a.privilege_type AS "privilege", a.is_grantable AS "grantable"
  FROM pg_database d
 CROSS JOIN LATERAL aclexplode(d.datacl) a
 WHERE pg_get_userbyid(a.grantee) = {who}
 ORDER BY 1, 2
"""),
        ("拥有的对象", f"""
SELECT n.nspname AS "schema", c.relname AS "object"
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r','p','v','m')
   AND n.nspname NOT IN ('pg_catalog','information_schema')
   AND pg_get_userbyid(c.relowner) = {who}
 ORDER BY 1, 2
"""),
        ("所属角色", f"""
SELECT b.rolname AS "role"
  FROM pg_auth_members m
  JOIN pg_roles b ON b.oid = m.roleid
  JOIN pg_roles r ON r.oid = m.member
 WHERE r.rolname = {who}
 ORDER BY 1
"""),
    ]


def privilege_matrix_sql(engine_kind: str, schema: str) -> str:
    """某个 schema / 库下「表 × 账号 × 权限」的全量明细，前端据此透视成矩阵。

    表的 `relacl` 为 NULL 表示「只有 owner 有权限、没给任何人授权」——这正是
    drama-u 上 62 张表 reader 一张都读不到的原因，所以 owner 必须一起返回，
    否则页面上会显示成一片空白而看不出「不是没查到，是压根没授权」。
    """
    check_engine(engine_kind)
    if engine_kind == "postgres":
        validate_name(schema, "postgres", "schema 名")
        target = quote_literal(schema, "postgres")
        return f"""
SELECT c.relname                        AS "table",
       pg_get_userbyid(c.relowner)      AS "owner",
       CASE WHEN a.grantee = 0 THEN 'PUBLIC'
            WHEN a.grantee IS NULL THEN NULL
            ELSE pg_get_userbyid(a.grantee) END AS "grantee",
       a.privilege_type                 AS "privilege"
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN LATERAL aclexplode(c.relacl) a ON TRUE
 WHERE n.nspname = {target}
   AND c.relkind IN ('r','p','v','m')
 ORDER BY 1, 3, 4
"""
    validate_name(schema, "mysql", "库名")
    target = quote_literal(schema, "mysql")
    # MySQL 没有 owner 概念；库级授权（`db`.*）对库内所有表生效，用 '*' 占位一并返回
    return f"""
SELECT tp.table_name AS `table`, NULL AS `owner`, tp.grantee AS `grantee`,
       tp.privilege_type AS `privilege`
  FROM information_schema.table_privileges tp
 WHERE tp.table_schema = {target}
 UNION ALL
SELECT '*', NULL, sp.grantee, sp.privilege_type
  FROM information_schema.schema_privileges sp
 WHERE sp.table_schema = {target}
 ORDER BY 1, 3, 4
"""


# ---------------------------------------------------------------- DCL 构造

def create_user(engine_kind: str, name: str, password: str, *,
                host: str = "%", can_login: bool = True) -> DclStatement:
    check_engine(engine_kind)
    if not password:
        raise PrivilegeError("请设置密码")
    if engine_kind == "mysql":
        name = validate_name(name, "mysql", "用户名")
        host = validate_host(host)
        acct = mysql_account(name, host)
        return DclStatement(
            sql=f"CREATE USER {acct} IDENTIFIED BY {quote_literal(password, 'mysql')}",
            audit_sql=f"CREATE USER {acct} IDENTIFIED BY {REDACTED}",
            summary=f"新建 MySQL 用户 {name}@{host}")
    name = validate_name(name, "postgres", "角色名")
    ident = quote_ident(name, "postgres")
    login = "LOGIN" if can_login else "NOLOGIN"
    return DclStatement(
        sql=f"CREATE ROLE {ident} WITH {login} PASSWORD {quote_literal(password, 'postgres')}",
        audit_sql=f"CREATE ROLE {ident} WITH {login} PASSWORD {REDACTED}",
        summary=f"新建 PostgreSQL 角色 {name}（{login}）")


def set_password(engine_kind: str, name: str, password: str, *, host: str = "%") -> DclStatement:
    check_engine(engine_kind)
    if not password:
        raise PrivilegeError("请设置密码")
    if engine_kind == "mysql":
        acct = mysql_account(validate_name(name, "mysql", "用户名"), validate_host(host))
        return DclStatement(
            sql=f"ALTER USER {acct} IDENTIFIED BY {quote_literal(password, 'mysql')}",
            audit_sql=f"ALTER USER {acct} IDENTIFIED BY {REDACTED}",
            summary=f"重置 {name}@{host} 的密码")
    ident = quote_ident(validate_name(name, "postgres", "角色名"), "postgres")
    return DclStatement(
        sql=f"ALTER ROLE {ident} WITH PASSWORD {quote_literal(password, 'postgres')}",
        audit_sql=f"ALTER ROLE {ident} WITH PASSWORD {REDACTED}",
        summary=f"重置角色 {name} 的密码")


def drop_user(engine_kind: str, name: str, *, host: str = "%") -> DclStatement:
    check_engine(engine_kind)
    if engine_kind == "mysql":
        name = validate_name(name, "mysql", "用户名")
        host = validate_host(host)
        sql = f"DROP USER {mysql_account(name, host)}"
        return DclStatement(sql=sql, audit_sql=sql, summary=f"删除 MySQL 用户 {name}@{host}")
    name = validate_name(name, "postgres", "角色名")
    # 不加 CASCADE：PG 的 DROP ROLE 在角色还持有对象/授权时会明确报错，
    # 这个报错正是我们要让人看见的（先 REASSIGN OWNED / DROP OWNED），不该悄悄绕过。
    sql = f"DROP ROLE {quote_ident(name, 'postgres')}"
    return DclStatement(sql=sql, audit_sql=sql, summary=f"删除 PostgreSQL 角色 {name}")


def _pg_target(level: str, schema: str, table: str, database: str) -> tuple[str, str]:
    """返回 (ON 子句, 人话描述)。"""
    if level == "table":
        s = validate_name(schema, "postgres", "schema 名")
        t = validate_name(table, "postgres", "表名")
        q = f'{quote_ident(s, "postgres")}.{quote_ident(t, "postgres")}'
        return f"ON TABLE {q}", f"表 {s}.{t}"
    if level == "all_tables":
        s = validate_name(schema, "postgres", "schema 名")
        return (f'ON ALL TABLES IN SCHEMA {quote_ident(s, "postgres")}',
                f"schema {s} 下的全部现有表")
    if level == "schema":
        s = validate_name(schema, "postgres", "schema 名")
        return f'ON SCHEMA {quote_ident(s, "postgres")}', f"schema {s}"
    if level == "database":
        d = validate_name(database, "postgres", "库名")
        return f'ON DATABASE {quote_ident(d, "postgres")}', f"数据库 {d}"
    raise PrivilegeError(f"不支持的授权层级：{level}")


def _mysql_target(level: str, database: str, table: str) -> tuple[str, str]:
    if level == "table":
        d = validate_name(database, "mysql", "库名")
        t = validate_name(table, "mysql", "表名")
        return (f'ON {quote_ident(d, "mysql")}.{quote_ident(t, "mysql")}', f"表 {d}.{t}")
    if level == "database":
        d = validate_name(database, "mysql", "库名")
        return f'ON {quote_ident(d, "mysql")}.*', f"库 {d} 的全部表"
    if level == "global":
        return "ON *.*", "全局（所有库）"
    raise PrivilegeError(f"MySQL 不支持的授权层级：{level}")


# 页面层级 → 白名单层级（PG 的 table / all_tables 共用一张表级白名单）
_LEVEL_TO_PRIV_SCOPE = {"table": "table", "all_tables": "table", "schema": "schema",
                        "database": "database", "global": "global"}


def grant(engine_kind: str, *, privileges: list[str], level: str, grantee: str,
          schema: str = "", table: str = "", database: str = "", host: str = "%",
          with_grant: bool = False, revoke: bool = False) -> DclStatement:
    """构造 GRANT / REVOKE。`revoke=True` 时生成对应的 REVOKE。"""
    check_engine(engine_kind)
    scope = _LEVEL_TO_PRIV_SCOPE.get(level)
    if scope is None:
        raise PrivilegeError(f"不支持的授权层级：{level}")
    privs = ", ".join(normalize_privileges(privileges, engine_kind, scope))
    if engine_kind == "mysql":
        who = mysql_account(validate_name(grantee, "mysql", "用户名"), validate_host(host))
        who_label = f"{grantee}@{validate_host(host)}"
        on_clause, target_label = _mysql_target(level, database, table)
    else:
        who = quote_ident(validate_name(grantee, "postgres", "角色名"), "postgres")
        who_label = grantee
        on_clause, target_label = _pg_target(level, schema, table, database)
    if revoke:
        sql = f"REVOKE {privs} {on_clause} FROM {who}"
        summary = f"收回 {who_label} 在{target_label}上的 {privs}"
    else:
        sql = f"GRANT {privs} {on_clause} TO {who}"
        if with_grant:
            sql += " WITH GRANT OPTION"
        summary = f"授予 {who_label} 在{target_label}上的 {privs}"
    return DclStatement(sql=sql, audit_sql=sql, summary=summary)


def default_privileges(*, privileges: list[str], schema: str, grantee: str,
                       obj_type: str = "TABLES", for_role: str = "",
                       revoke: bool = False) -> DclStatement:
    """PG 专属：ALTER DEFAULT PRIVILEGES——让**以后新建**的对象自动带上授权。

    只 GRANT ON ALL TABLES 是一次性的：之后新建的表仍然没授权，人会以为「授过了怎么又不行」。
    注意 `FOR ROLE` 指的是**建表者**，不写则是当前连接的角色；建表者不是当前角色时必须显式指定。
    """
    obj = (obj_type or "TABLES").strip().upper()
    if obj not in _PG_DEFAULT_OBJ:
        raise PrivilegeError(f"对象类型 {obj_type!r} 不支持（允许：{', '.join(_PG_DEFAULT_OBJ)}）")
    scope = {"TABLES": "table", "SEQUENCES": "sequence", "SCHEMAS": "schema"}.get(obj, "table")
    privs = ", ".join(normalize_privileges(privileges, "postgres", scope))
    s = validate_name(schema, "postgres", "schema 名")
    who = quote_ident(validate_name(grantee, "postgres", "角色名"), "postgres")
    head = "ALTER DEFAULT PRIVILEGES"
    owner_label = ""
    if for_role:
        head += f' FOR ROLE {quote_ident(validate_name(for_role, "postgres", "建表者角色"), "postgres")}'
        owner_label = f"由 {for_role} 建的"
    head += f' IN SCHEMA {quote_ident(s, "postgres")}'
    verb = f"REVOKE {privs} ON {obj} FROM {who}" if revoke else f"GRANT {privs} ON {obj} TO {who}"
    action = "取消" if revoke else "设置"
    return DclStatement(
        sql=f"{head} {verb}",
        audit_sql=f"{head} {verb}",
        summary=f"{action}默认权限：{s} 下今后{owner_label}新建的 {obj} 自动给 {grantee} {privs}")


# 动作名 → 构造函数。admin 路由只认这张表里的动作，杜绝「传什么执行什么」。
def build(engine_kind: str, action: str, params: dict) -> DclStatement:
    """按动作名分发到具体构造函数。未知动作直接拒。"""
    check_engine(engine_kind)
    p = params or {}
    if action == "create_user":
        return create_user(engine_kind, p.get("name", ""), p.get("password", ""),
                           host=p.get("host", "%"), can_login=bool(p.get("can_login", True)))
    if action == "set_password":
        return set_password(engine_kind, p.get("name", ""), p.get("password", ""),
                            host=p.get("host", "%"))
    if action == "drop_user":
        return drop_user(engine_kind, p.get("name", ""), host=p.get("host", "%"))
    if action in ("grant", "revoke"):
        return grant(engine_kind, privileges=p.get("privileges") or [],
                     level=p.get("level", ""), grantee=p.get("grantee", ""),
                     schema=p.get("schema", ""), table=p.get("table", ""),
                     database=p.get("database", ""), host=p.get("host", "%"),
                     with_grant=bool(p.get("with_grant")), revoke=(action == "revoke"))
    if action in ("default_privileges", "revoke_default_privileges"):
        if engine_kind != "postgres":
            raise PrivilegeError("默认权限（ALTER DEFAULT PRIVILEGES）是 PostgreSQL 专属能力")
        return default_privileges(privileges=p.get("privileges") or [],
                                  schema=p.get("schema", ""), grantee=p.get("grantee", ""),
                                  obj_type=p.get("obj_type", "TABLES"),
                                  for_role=p.get("for_role", ""),
                                  revoke=(action == "revoke_default_privileges"))
    raise PrivilegeError(f"未知的权限操作：{action}")


def available_privileges(engine_kind: str) -> dict[str, list[str]]:
    """给前端渲染多选框用：每个层级可选的权限。"""
    check_engine(engine_kind)
    table = _PG_PRIVS if engine_kind == "postgres" else _MYSQL_PRIVS
    return {level: list(privs) for level, privs in table.items()}
