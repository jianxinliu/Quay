"""DuckDB 驱动（仅元数据，不可连接）。

分析工作台把外部库的查询结果快照进一个**进程内** DuckDB 实例做跨源 JOIN/聚合
（见 analysis.py），它不是一种「连上去的数据库」：没有连接配置、没有账号、没有池。
注册进来的唯一目的是让方言（sqlglot 美化/编辑器 lint）、图标与「可 lint 的引擎」
清单跟真实引擎走**同一个来源**——加一种进程内引擎时改这里，不用去 admin 各处加清单。

build_engine 刻意不实现：配置里出现 duckdb 连接是无意义的，直接报错比静默跑好。
"""

from __future__ import annotations

from .base import DbDriver, register


@register
class DuckdbDriver(DbDriver):
    name = "duckdb"
    dialect = "duckdb"
    connectable = False
    # 连接表单里不会出现它（build_engine 刻意不实现），自然也不能做同步目标
    sync_target = False
    icon = "duckdb"
    client_lib = ("duckdb", "duckdb")
    # 不会被 engine.dialect.name 反查到（没有 SQLAlchemy 连接）
    sa_dialect_names = ()
