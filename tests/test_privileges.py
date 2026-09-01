"""用户与权限管理的纯函数层：注入防线、方言差异、密码不落审计。

这层的输入全部来自页面表单，会被拼进标识符与关键字位置，所以「拒绝非法输入」
和「正确引用」两道防线都要有测试盯着。
"""

import pytest

from dbmcp.privileges import (
    DclStatement,
    PrivilegeError,
    available_privileges,
    build,
    create_user,
    default_privileges,
    drop_user,
    grant,
    list_users_sql,
    normalize_privileges,
    privilege_matrix_sql,
    quote_ident,
    quote_literal,
    set_password,
    user_grants_sql,
    validate_host,
    validate_name,
)


class TestValidation:
    def test_rejects_injection_shaped_names(self):
        for bad in ('a"; DROP ROLE x; --', "a'--", "a`b", "a b", "a;b", "a\nb", "", "  ",
                    "-lead", "a\\b", "a/*c*/"):
            with pytest.raises(PrivilegeError):
                validate_name(bad, "postgres")

    def test_accepts_ordinary_names(self):
        for good in ("drama", "app_read", "svc-1", "a.b", "u$1", "R2"):
            assert validate_name(good, "postgres") == good

    def test_length_capped_per_engine(self):
        assert validate_name("a" * 63, "postgres")
        with pytest.raises(PrivilegeError):
            validate_name("a" * 64, "postgres")
        assert validate_name("a" * 64, "mysql")
        with pytest.raises(PrivilegeError):
            validate_name("a" * 65, "mysql")

    def test_host_defaults_to_any_and_rejects_junk(self):
        assert validate_host("") == "%"
        assert validate_host("10.0.%") == "10.0.%"
        with pytest.raises(PrivilegeError):
            validate_host("h'; DROP USER x; --")

    def test_unsupported_engine_rejected(self):
        with pytest.raises(PrivilegeError):
            list_users_sql("clickhouse")
        with pytest.raises(PrivilegeError):
            build("sqlite", "create_user", {"name": "a", "password": "b"})


class TestQuoting:
    def test_dialect_identifier_quotes(self):
        assert quote_ident("tbl", "postgres") == '"tbl"'
        assert quote_ident("tbl", "mysql") == "`tbl`"

    def test_quote_doubles_embedded_quote(self):
        assert quote_ident('a"b', "postgres") == '"a""b"'
        assert quote_ident("a`b", "mysql") == "`a``b`"

    def test_literal_escapes_quote_and_mysql_backslash(self):
        assert quote_literal("a'b", "postgres") == "'a''b'"
        # MySQL 默认开 backslash escape，反斜杠必须一并转义，否则 `\\'` 能逃逸出字面量
        assert quote_literal("a\\'b", "mysql") == "'a\\\\''b'"


class TestPrivilegeWhitelist:
    def test_unknown_privilege_rejected(self):
        with pytest.raises(PrivilegeError):
            normalize_privileges(["SELECT", "DROP DATABASE"], "postgres", "table")

    def test_injection_via_privilege_rejected(self):
        with pytest.raises(PrivilegeError):
            normalize_privileges(["SELECT ON x TO evil; --"], "postgres", "table")

    def test_normalizes_case_and_dedupes(self):
        assert normalize_privileges(["select", " Insert ", "SELECT"], "postgres",
                                    "table") == ["SELECT", "INSERT"]

    def test_all_absorbs_others(self):
        assert normalize_privileges(["SELECT", "ALL"], "postgres", "table") == ["ALL"]
        assert normalize_privileges(["SELECT", "ALL PRIVILEGES"], "mysql",
                                    "table") == ["ALL PRIVILEGES"]

    def test_empty_selection_rejected(self):
        with pytest.raises(PrivilegeError):
            normalize_privileges([], "postgres", "table")

    def test_level_specific_whitelists(self):
        # USAGE 是 schema 级权限，表级不接受
        normalize_privileges(["USAGE"], "postgres", "schema")
        with pytest.raises(PrivilegeError):
            normalize_privileges(["USAGE"], "postgres", "table")

    def test_available_privileges_shape(self):
        pg = available_privileges("postgres")
        assert "SELECT" in pg["table"] and "USAGE" in pg["schema"]
        assert "global" in available_privileges("mysql")


class TestPasswordNeverAudited:
    """红线 2：密码永不出现在日志、审计记录、返回值中。"""

    def test_create_user_redacts_password(self):
        st = create_user("postgres", "app", "hunter2")
        assert "hunter2" in st.sql            # 真正执行的语句里当然要有
        assert "hunter2" not in st.audit_sql  # 落审计的绝不能有
        assert "***" in st.audit_sql
        assert st.has_secret

    def test_set_password_redacts(self):
        st = set_password("mysql", "app", "hunter2", host="10.0.%")
        assert "hunter2" not in st.audit_sql and "hunter2" in st.sql

    def test_password_with_quotes_is_escaped_not_broken(self):
        st = create_user("mysql", "app", "a'b\\c")
        assert st.sql.endswith("IDENTIFIED BY 'a''b\\\\c'")

    def test_statements_without_secrets_are_identical(self):
        st = grant("postgres", privileges=["USAGE"], level="schema",
                   grantee="app", schema="public")
        assert st.sql == st.audit_sql and not st.has_secret

    def test_empty_password_rejected(self):
        with pytest.raises(PrivilegeError):
            create_user("postgres", "app", "")


class TestPostgresStatements:
    def test_create_role(self):
        assert create_user("postgres", "app", "pw").sql == (
            'CREATE ROLE "app" WITH LOGIN PASSWORD \'pw\'')

    def test_create_nologin_role(self):
        assert 'NOLOGIN' in create_user("postgres", "grp", "pw", can_login=False).sql

    def test_drop_role_has_no_cascade(self):
        # 有依赖对象时 PG 会明确报错——那个报错正是要让人看见的，不该悄悄 CASCADE 掉
        st = drop_user("postgres", "app")
        assert st.sql == 'DROP ROLE "app"' and "CASCADE" not in st.sql

    def test_grant_all_tables_in_schema(self):
        st = grant("postgres", privileges=["SELECT"], level="all_tables",
                   grantee="drama", schema="public")
        assert st.sql == 'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "drama"'

    def test_grant_single_table_is_schema_qualified(self):
        st = grant("postgres", privileges=["SELECT", "UPDATE"], level="table",
                   grantee="app", schema="public", table="crawl_job")
        assert st.sql == 'GRANT SELECT, UPDATE ON TABLE "public"."crawl_job" TO "app"'

    def test_grant_with_grant_option(self):
        st = grant("postgres", privileges=["USAGE"], level="schema", grantee="app",
                   schema="public", with_grant=True)
        assert st.sql.endswith("WITH GRANT OPTION")

    def test_revoke_uses_from(self):
        st = grant("postgres", privileges=["SELECT"], level="all_tables", grantee="app",
                   schema="public", revoke=True)
        assert st.sql.startswith("REVOKE SELECT ") and st.sql.endswith('FROM "app"')

    def test_default_privileges_statement(self):
        st = default_privileges(privileges=["SELECT"], schema="public", grantee="drama",
                                for_role="root")
        assert st.sql == ('ALTER DEFAULT PRIVILEGES FOR ROLE "root" IN SCHEMA "public" '
                          'GRANT SELECT ON TABLES TO "drama"')

    def test_default_privileges_revoke_and_obj_type(self):
        st = default_privileges(privileges=["USAGE"], schema="public", grantee="app",
                                obj_type="sequences", revoke=True)
        assert "REVOKE USAGE ON SEQUENCES FROM \"app\"" in st.sql

    def test_default_privileges_rejects_unknown_obj_type(self):
        with pytest.raises(PrivilegeError):
            default_privileges(privileges=["SELECT"], schema="public", grantee="app",
                               obj_type="ROWS")

    def test_default_privileges_is_pg_only(self):
        with pytest.raises(PrivilegeError):
            build("mysql", "default_privileges",
                  {"privileges": ["SELECT"], "schema": "d", "grantee": "u"})


class TestMysqlStatements:
    def test_create_user_uses_account_literal_form(self):
        st = create_user("mysql", "app", "pw", host="10.0.%")
        assert st.sql == "CREATE USER 'app'@'10.0.%' IDENTIFIED BY 'pw'"

    def test_grant_database_level(self):
        st = grant("mysql", privileges=["SELECT"], level="database", grantee="app",
                   database="shop", host="%")
        assert st.sql == "GRANT SELECT ON `shop`.* TO 'app'@'%'"

    def test_grant_table_level(self):
        st = grant("mysql", privileges=["SELECT"], level="table", grantee="app",
                   database="shop", table="orders")
        assert st.sql == "GRANT SELECT ON `shop`.`orders` TO 'app'@'%'"

    def test_grant_global_level(self):
        st = grant("mysql", privileges=["PROCESS"], level="global", grantee="app")
        assert st.sql == "GRANT PROCESS ON *.* TO 'app'@'%'"

    def test_pg_only_levels_rejected_on_mysql(self):
        with pytest.raises(PrivilegeError):
            grant("mysql", privileges=["USAGE"], level="schema", grantee="app", schema="s")

    def test_drop_user_includes_host(self):
        assert drop_user("mysql", "app", host="localhost").sql == "DROP USER 'app'@'localhost'"


class TestCatalogQueries:
    def test_list_users_targets_right_catalog(self):
        assert "pg_roles" in list_users_sql("postgres")
        assert "mysql.user" in list_users_sql("mysql")

    def test_pg_grants_use_aclexplode_not_information_schema(self):
        # information_schema 只反映「与当前登录角色相关」的授权，看不到任意账号的全貌
        groups = user_grants_sql("postgres", "drama")
        joined = " ".join(sql for _, sql in groups)
        assert "aclexplode" in joined and "information_schema.role_table_grants" not in joined
        assert [t for t, _ in groups] == ["表 / 视图", "Schema", "数据库", "拥有的对象", "所属角色"]

    def test_mysql_grants_use_show_grants(self):
        (title, sql), = user_grants_sql("mysql", "app", "10.0.%")
        assert sql == "SHOW GRANTS FOR 'app'@'10.0.%'"

    def test_grants_reject_bad_user_name(self):
        with pytest.raises(PrivilegeError):
            user_grants_sql("postgres", "a'; DROP ROLE x; --")
        with pytest.raises(PrivilegeError):
            user_grants_sql("mysql", "a'@'%'; DROP USER x; --")

    def test_matrix_returns_owner_so_empty_acl_is_explainable(self):
        # relacl 为 NULL = 只有 owner 有权限。不带 owner 的话页面只剩一片空白，
        # 看不出「不是没查到，是压根没授权」——drama-u 上 62 张表就是这种情况。
        sql = privilege_matrix_sql("postgres", "public")
        assert "relowner" in sql and "LEFT JOIN LATERAL" in sql
        assert "'PUBLIC'" in sql  # grantee=0 是 PUBLIC，pg_get_userbyid(0) 会报错

    def test_matrix_rejects_bad_schema(self):
        with pytest.raises(PrivilegeError):
            privilege_matrix_sql("postgres", "public'; DROP SCHEMA x; --")

    def test_mysql_matrix_covers_schema_level_grants(self):
        sql = privilege_matrix_sql("mysql", "shop")
        assert "table_privileges" in sql and "schema_privileges" in sql


class TestBuildDispatch:
    def test_unknown_action_rejected(self):
        with pytest.raises(PrivilegeError):
            build("postgres", "make_superuser", {"name": "x"})

    def test_dispatch_returns_dcl_statement(self):
        st = build("postgres", "grant", {"privileges": ["SELECT"], "level": "all_tables",
                                         "grantee": "drama", "schema": "public"})
        assert isinstance(st, DclStatement)
        assert st.summary.startswith("授予 drama")

    def test_revoke_dispatch(self):
        st = build("mysql", "revoke", {"privileges": ["SELECT"], "level": "database",
                                       "grantee": "app", "database": "shop"})
        assert st.sql.startswith("REVOKE SELECT ON `shop`.* FROM")
        assert st.summary.startswith("收回 app@%")
