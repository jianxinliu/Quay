"""驱动注册表入口。

import 本包即完成内置驱动的注册（各驱动模块在模块级调用 register）。
新增引擎支持：在 drivers/ 下加一个模块、在这里加一行 import 即可，config/engine
类型已放宽为 str，不必改别处。
"""

from __future__ import annotations

from .base import (
    DB_CLIENT_NAME,
    DRIVERS,
    DbDriver,
    Role,
    SyntaxCheck,
    UnsupportedEngineError,
    driver_for_engine,
    first_sql_keyword,
    connectable_engines,
    engine_default_port,
    engine_dialect,
    get_driver,
    register,
    resolve_account,
    role_timeouts,
    supported_engines,
)

# 内置驱动：import 即注册
from . import clickhouse as _clickhouse  # noqa: F401  (register on import)
from . import duckdb as _duckdb  # noqa: F401  (进程内分析引擎，仅元数据)
from . import mysql as _mysql  # noqa: F401
from . import postgres as _postgres  # noqa: F401
from . import sqlite as _sqlite  # noqa: F401

__all__ = [
    "DB_CLIENT_NAME",
    "DRIVERS",
    "DbDriver",
    "Role",
    "SyntaxCheck",
    "UnsupportedEngineError",
    "driver_for_engine",
    "first_sql_keyword",
    "connectable_engines",
    "engine_default_port",
    "engine_dialect",
    "get_driver",
    "register",
    "resolve_account",
    "role_timeouts",
    "supported_engines",
]
