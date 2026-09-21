"""可插拔数据库驱动：把「一种引擎」封装成一个独立的适配单元。

设计目标
========
支持这么多 DB，引擎相关行为以前散落在 engines.py 的 if/elif 和各处
``engine_kind ==`` 分支里（连接构建、取消、DDL、容量估算、语法复核、体检…），
加一个引擎要改十几个地方。现在收拢成 ``DbDriver`` 一个接口 + 一个注册表：

- **后续加 DB 支持就加驱动**：新增 ``src/dbmcp/drivers/xxx.py``，实现需要的部分、
  在本包 ``__init__`` 里 import 一下完成注册即可。``config.engine`` 已放宽为 str，
  不用再改类型定义。
- **驱动可以自己写，也可以用成熟驱动库**：关系库基本零代码——SQLAlchemy dialect
  覆盖了 MySQL/PG/SQLite/ClickHouse/MSSQL/Oracle/DB2…，``build_engine`` 就是拼 URL；
  没有现成方言的库（Redis 是先例，见 redis_engine.py）就自己实现接口的几个方法。

基类的方法都给了**能用的默认实现**：取消为空操作、容量/行数估算返回空、DDL 走反射
拼近似、语法复核标为不支持。新引擎只需覆盖自己真正支持的部分，缺的能力会优雅降级
（前端/调用方本来就按「取不到 = 不支持」处理），而不会报错。

模块依赖方向
============
drivers →（运行时惰性）engines；engines → drivers（注册表）。drivers 的模块级
代码绝不 import engines（只在方法内惰性 import），故 import 无环。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import Engine as SAEngine

from .. import __version__
from ..config import ConnectionConfig

Role = Literal["reader", "writer"]

# 展示在 DB 侧的客户端名（performance_schema.session_connect_attrs /
# pg_stat_activity.application_name / system.processes.client_name）
DB_CLIENT_NAME = f"Quay({__version__})"


class UnsupportedEngineError(Exception):
    """引擎未注册，或该引擎的驱动不支持当前操作。"""


@dataclass(frozen=True)
class SyntaxCheck:
    """DB 侧语法复核结果。

    supported=False：该引擎/语句无法复核，调用方不应据此判死。
    supported=True 且 ok=False：DB 明确报了语法错，error 是已脱敏的错误摘要。
    """

    supported: bool
    ok: bool
    error: str = ""
    stmt_index: int = 0   # 多语句批量时，出错的是第几条（从 1 计）


_SYNTAX_CHECK_STMT_NAME = "dbm_syntax_chk"


def first_sql_keyword(sql: str) -> str:
    """取 SQL 的首个关键字（跳过前导注释/空白），小写返回。空串表示取不到。"""
    text_ = sql.strip()
    while True:
        if text_.startswith("--"):
            nl = text_.find("\n")
            if nl < 0:
                return ""
            text_ = text_[nl + 1:].lstrip()
            continue
        if text_.startswith("/*"):
            end = text_.find("*/")
            if end < 0:
                return ""
            text_ = text_[end + 2:].lstrip()
            continue
        break
    m = re.match(r"[A-Za-z_]+", text_)
    return m.group(0).lower() if m else ""


def resolve_account(cfg: ConnectionConfig, role: Role) -> tuple[str, str]:
    """返回 (user, password_ref)。writer 角色要求配置了 writer 账号。"""
    if role == "writer":
        if cfg.writer is None:
            raise UnsupportedEngineError("该连接未配置 writer 账号，无法执行写操作")
        return cfg.writer.user, cfg.writer.password
    return cfg.user or "", cfg.password or ""


def role_timeouts(policy, readonly: bool) -> tuple[int, int]:  # noqa: ANN001
    """按角色算 (语句超时, socket/写操作超时)。

    - 语句超时（stmt）：始终用 policy.statement_timeout_s，喂 MySQL max_execution_time（只约束 SELECT）。
    - 操作超时（op）：reader 用语句超时；writer 用 policy.write_timeout_s——放大给大
      DELETE/UPDATE 留足时间，否则 reader 的 30s socket read_timeout 会把长写打成
      pymysql 2013「Lost connection ... read operation timed out」。MySQL 用作
      socket read/write_timeout；PG 用作 writer 的 statement_timeout。
    """
    stmt = policy.statement_timeout_s
    return stmt, (stmt if readonly else policy.write_timeout_s)


class DbDriver:
    """一种数据库引擎的适配单元。新增引擎 = 新增一个驱动并注册（见模块文档）。

    所有方法都是「引擎特有行为」；引擎无关的执行/反射/类型归一仍在 engines.py，
    驱动通过运行时惰性 import 复用（避免模块级循环依赖）。
    """

    # ---- 身份 ----
    name: str = ""                 # 引擎标识 = config.engine 的取值
    dialect: str | None = None     # sqlglot 方言；None 表示不静态解析
    sa_dialect_names: tuple[str, ...] = ()   # engine.dialect.name 的可能取值（用于从已建好的引擎反查驱动）
    default_port: int | None = None           # 后台表单的默认端口联动
    # 查询台是否需要「先选库」这一层（PG 的 database/schema 是两层；MySQL/CH 的 schema 就是库）
    needs_database_layer: bool = False
    # 能否作为**连接**配置。分析工作台的进程内 DuckDB 不是一种可连数据库，
    # 注册进来的目的只是让方言/图标/可 lint 清单与真实引擎同一份来源（连接表单会过滤掉它）。
    connectable: bool = True
    icon: str | None = None                  # 品牌图标文件名（devicon，vendored 到 static/db-icons/）；None = 无图标

    # ---- 能力声明：新引擎按实际情况覆盖，未声明的能力按默认值处理 ----
    # 表位于 schema/database 之下：未绑定默认库时反射会崩或落错库，故未选库时
    # 调用方应强制先选库（sqlite 一个文件就是一个库，不需要这一层）
    has_schema_layer: bool = True
    # 可作为表同步的目标（要能执行 CREATE TABLE + INSERT）。ClickHouse 本项目只读，不做目标
    sync_target: bool = True
    # AI 生成 SQL/DAG：按本引擎方言生成并转写；方言覆盖不全的引擎先关掉，避免生成跑不了的 SQL
    ai_sql: bool = True

    # ---- 执行计划 ----
    explain_prefix: str = "EXPLAIN "             # 审批流文本计划前缀
    explain_json_prefix: str | None = None       # 查询台 JSON 计划前缀；None = 该引擎不支持
    explain_format: Literal["rows", "json"] = "rows"

    # ---- 建连 ----
    def build_engine(
        self,
        cfg: ConnectionConfig,
        role: Role,
        host: str | None,
        port: int | None,
        schema: str | None = None,
        pool_size: int = 15,
        database: str | None = None,
    ) -> SAEngine:
        """创建引擎。

        schema：查询台执行 schema 上下文（MySQL 覆盖默认库；PG 设 search_path）。
        database：**仅 needs_database_layer 的引擎**——选定这条连接要绑的 database
        （PG 一条连接只能绑一个，换库只能换连接）。
        """
        raise UnsupportedEngineError(f"驱动 {self.name!r} 未实现 build_engine")

    # ---- 运行期取消 ----
    def make_canceller(self, engine: SAEngine, sa_conn) -> Callable[[], None]:  # noqa: ANN001
        """为一次正在执行的查询构造「取消函数」：在 DB 层中断它，而不是杀线程。

        默认空操作——取不到连接标识或引擎不支持时，取消对排队任务生效、对运行中无害。
        """
        return lambda: None

    # ---- 元数据 / 检索 ----
    def search_tables(self, engine: SAEngine, q: str, limit: int = 50) -> list[dict]:
        """跨库按名模糊搜表（查询台 ⌘P 跳转）。返回 [{db, table}]。默认不支持。"""
        return []

    def list_server_databases(self, engine: SAEngine) -> list[str]:
        """列出**服务器上的 database**（不是 schema）。默认复用 engines.list_databases。"""
        from ..engines import list_databases  # 惰性：engines 反过来 import 本包做注册

        return list_databases(engine)

    def table_sizes(self, engine: SAEngine, schema: str | None = None) -> dict[str, int]:
        """按表返回存储容量（字节，数据+索引）。取不到返回空 dict（不阻断）。"""
        return {}

    def estimate_row_count(self, engine: SAEngine, table: str,
                           schema: str | None = None) -> int | None:
        """表行数量级估算（优先引擎统计，避免大表全表 count）。None 表示无法估算。"""
        return None

    def get_table_ddl(self, engine: SAEngine, table: str,
                      schema: str | None = None) -> str:
        """取建表语句。默认：引擎无 SHOW CREATE TABLE 时由反射拼近似 DDL。"""
        from ..engines import describe_table

        info = describe_table(engine, table, schema)
        body = []
        for c in info["columns"]:
            line = f"  {c['name']} {c['type']}"
            if not c.get("nullable", True):
                line += " NOT NULL"
            if c.get("default") is not None:
                line += f" DEFAULT {c['default']}"
            body.append(line)
        if info.get("primary_key"):
            body.append("  PRIMARY KEY (" + ", ".join(info["primary_key"]) + ")")
        qname = f"{schema}.{table}" if schema else table
        out = ["-- 由表结构反射生成的近似 DDL（该引擎无 SHOW CREATE TABLE）",
               f"CREATE TABLE {qname} (", ",\n".join(body), ");"]
        for i in info.get("indexes", []):
            uniq = "UNIQUE " if i.get("unique") else ""
            cols = ", ".join(i.get("columns") or [])
            out.append(f"CREATE {uniq}INDEX {i['name']} ON {qname} ({cols});")
        return "\n".join(out)

    # ---- DB 侧语法复核（只解析不执行）----
    def syntax_check_supported(self, stmt: str) -> bool:
        """这条语句该不该送去 DB 复核（按引擎能力与语句类型判断）。默认不支持。"""
        return False

    def check_statement(self, conn, stmt: str) -> SyntaxCheck:  # noqa: ANN001
        """在已有连接上复核单条语句。**只解析不执行**，不提交任何事务。"""
        return SyntaxCheck(supported=False, ok=True)



# =====================================================================
# 注册表
# =====================================================================
DRIVERS: dict[str, DbDriver] = {}


def register(driver: "DbDriver | type[DbDriver]") -> DbDriver:
    """注册一个驱动（可直接传实例，也可传类——类会被无参实例化，驱动应当无状态）。

    重复注册（同名）以最后一次为准——便于测试时替换。
    """
    if isinstance(driver, type):
        driver = driver()
    if not driver.name:
        raise UnsupportedEngineError("驱动必须提供非空 name")
    DRIVERS[driver.name] = driver
    return driver


def supported_engines() -> list[str]:
    """已注册的引擎清单（排序输出）。"""
    return sorted(DRIVERS)


def connectable_engines() -> list[str]:
    """可作为**连接**的引擎（连接表单、同步源候选等用）。

    排除两类：不可连接的驱动（如分析工作台的进程内 DuckDB），以及 redis 那种有独立
    适配器、不在本注册表里的引擎——后者由调用方按需补上。
    """
    return [e for e in sorted(DRIVERS) if DRIVERS[e].connectable]


def engine_dialect(engine: str) -> str | None:
    """sqlglot 方言名（SQL 美化 / 编辑器 lint / 同步转写用）。未注册或无方言返回 None。"""
    try:
        return DRIVERS[engine].dialect
    except KeyError:
        return None


def engine_default_port(engine: str) -> int | None:
    """该引擎的默认端口。未注册的引擎（redis 等）返回 None，由调用方自行兜底。"""
    try:
        return DRIVERS[engine].default_port
    except KeyError:
        return None


def get_driver(engine: str) -> DbDriver:
    """按 config.engine 取驱动。未注册时给出已注册清单，提示「加驱动」而非「不支持」。"""
    drv = DRIVERS.get(engine)
    if drv is None:
        raise UnsupportedEngineError(
            f"引擎 {engine!r} 未注册驱动（已注册：{', '.join(supported_engines()) or '无'}）。"
            f"新增引擎支持：在 drivers/ 下加一个驱动模块并 import 注册")
    return drv


def engine_capabilities(engine: str) -> DbDriver | None:
    """取驱动；未注册返回 None（调用方自行决定降级还是拒绝）。"""
    return DRIVERS.get(engine)


def driver_for_engine(engine: SAEngine) -> DbDriver:
    """从一条**已建好的** SQLAlchemy 引擎反查它的驱动（按 engine.dialect.name 匹配）。

    用于运行期只知道引擎、不知道 config.engine 的地方（取消、执行计划等）。
    """
    name = engine.dialect.name
    for drv in DRIVERS.values():
        if name in drv.sa_dialect_names:
            return drv
    raise UnsupportedEngineError(
        f"SQLAlchemy 方言 {name!r} 没有对应驱动（已注册：{', '.join(supported_engines())}）")
