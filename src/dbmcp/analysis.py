"""分析工作台核心：DuckDB 工作区管理（设计见 ANALYSIS.md）。

- 每个工作区一个独立 .duckdb 文件（data/analysis/<name>.duckdb），文件系统即真相。
- 工作区内是"草稿纸"：任意 SQL（含 DDL/DML）自由执行，不需要审批——
  它不碰生产数据；**从源库取数**由 service 层走 reader + 审计 + 行数上限。
- DuckDB 连接非线程安全：每次操作短连接（connect → 用 → close），简单可靠。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_SNAPSHOT_ROWS = 500_000       # 快照行数硬上限（防拉挂源库/塞爆本地）
DEFAULT_SNAPSHOT_ROWS = 200_000   # 默认快照行数
MAX_RESULT_ROWS = 5_000           # 工作区查询默认返回上限（分页兜底仍由调用方做）

_NAME_RE = re.compile(r"^[a-zA-Z_][\w-]{0,63}$")


class AnalysisError(Exception):
    """工作区操作失败。message 面向使用者/agent。"""


def _valid_name(name: str, kind: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise AnalysisError(f"{kind}名只允许字母/数字/下划线/连字符（1-64 位），实际: {name!r}")
    return name


class AnalysisStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 工作区 ----------

    def _path(self, workspace: str) -> Path:
        return self.root / f"{_valid_name(workspace, '工作区')}.duckdb"

    def _connect(self, workspace: str, must_exist: bool = True):
        import duckdb  # noqa: PLC0415  惰性导入，未安装不影响其他功能

        path = self._path(workspace)
        if must_exist and not path.exists():
            raise AnalysisError(f"工作区 {workspace!r} 不存在，可先创建或用 import 自动创建")
        return duckdb.connect(str(path))

    def list_workspaces(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("*.duckdb")):
            out.append({"workspace": f.stem, "size_bytes": f.stat().st_size})
        return out

    def create_workspace(self, workspace: str) -> None:
        self._connect(workspace, must_exist=False).close()

    def drop_workspace(self, workspace: str) -> None:
        path = self._path(workspace)
        if not path.exists():
            raise AnalysisError(f"工作区 {workspace!r} 不存在")
        path.unlink()
        wal = path.with_suffix(".duckdb.wal")
        if wal.exists():
            wal.unlink()

    # ---------- 数据集 ----------

    def _save_provenance(self, con, dataset: str, spec: dict) -> None:
        """记录数据集的取数配方（workflow 重跑用）。"""
        import json
        con.execute("CREATE TABLE IF NOT EXISTS __provenance (dataset VARCHAR PRIMARY KEY, spec VARCHAR)")
        con.execute("DELETE FROM __provenance WHERE dataset = ?", [dataset])
        con.execute("INSERT INTO __provenance VALUES (?, ?)", [dataset, json.dumps(spec, ensure_ascii=False)])

    def get_provenance(self, workspace: str) -> list[dict]:
        """工作区各数据集的取数配方（无记录返回空）。"""
        import json
        con = self._connect(workspace)
        try:
            try:
                rows = con.execute("SELECT dataset, spec FROM __provenance ORDER BY dataset").fetchall()
            except Exception:
                return []
            return [{"dataset": r[0], **json.loads(r[1])} for r in rows]
        finally:
            con.close()

    def list_datasets(self, workspace: str) -> list[dict]:
        """工作区内的表与视图（数据集 + 虚拟表；内部 __ 表不展示）。"""
        con = self._connect(workspace)
        try:
            rows = con.execute(
                "SELECT table_name, table_type FROM information_schema.tables"
                " WHERE table_schema = 'main' AND table_name NOT LIKE '~_~_%' ESCAPE '~'"
                " ORDER BY table_name"
            ).fetchall()
            out = []
            for name, ttype in rows:
                n = con.execute(  # noqa: S608 表名来自 information_schema
                    f'SELECT count(*) FROM "{name}"').fetchone()[0]
                out.append({"name": name, "type": "view" if "VIEW" in ttype.upper() else "table",
                            "rows": int(n)})
            return out
        finally:
            con.close()

    def import_rows(
        self, workspace: str, dataset: str, columns: list[str], rows: list[list[Any]],
        replace: bool = True, spec: dict | None = None,
    ) -> int:
        """把（service 从源库拉到的）行集落成工作区的表。列类型按前 200 行推断。"""
        _valid_name(dataset, "数据集")
        if not columns:
            raise AnalysisError("结果集没有列，无法导入")
        con = self._connect(workspace, must_exist=False)
        try:
            types = _infer_types(columns, rows)
            cols_ddl = ", ".join(f'"{c}" {t}' for c, t in zip(columns, types, strict=True))
            if replace:
                con.execute(f'DROP TABLE IF EXISTS "{dataset}"')
                con.execute(f'DROP VIEW IF EXISTS "{dataset}"')
            con.execute(f'CREATE TABLE "{dataset}" ({cols_ddl})')
            if rows:
                ph = ", ".join(["?"] * len(columns))
                con.executemany(f'INSERT INTO "{dataset}" VALUES ({ph})',
                                [_coerce_row(r, types) for r in rows])
            if spec:
                self._save_provenance(con, dataset, spec)
            return len(rows)
        finally:
            con.close()

    def import_file(self, workspace: str, dataset: str, path: str, replace: bool = True,
                    record_spec: bool = True) -> int:
        """导入本地 CSV / Parquet / JSON 文件（DuckDB 原生读取，类型自动推断）。"""
        _valid_name(dataset, "数据集")
        p = Path(path).expanduser()
        if not p.is_file():
            raise AnalysisError(f"文件不存在: {p}")
        suffix = p.suffix.lower()
        reader = {
            ".csv": "read_csv_auto(?)",
            ".tsv": "read_csv_auto(?)",
            ".parquet": "read_parquet(?)",
            ".json": "read_json_auto(?)",
            ".jsonl": "read_json_auto(?)",
            ".ndjson": "read_json_auto(?)",
        }.get(suffix)
        if reader is None:
            raise AnalysisError(f"不支持的文件类型 {suffix}（支持 csv/tsv/parquet/json/jsonl）")
        con = self._connect(workspace, must_exist=False)
        try:
            if replace:
                con.execute(f'DROP TABLE IF EXISTS "{dataset}"')
                con.execute(f'DROP VIEW IF EXISTS "{dataset}"')
            con.execute(f'CREATE TABLE "{dataset}" AS SELECT * FROM {reader}', [str(p)])
            if record_spec:
                self._save_provenance(con, dataset, {"kind": "file", "path": str(p)})
            return int(con.execute(f'SELECT count(*) FROM "{dataset}"').fetchone()[0])
        finally:
            con.close()

    def run_sql(self, workspace: str, sql: str, max_rows: int = MAX_RESULT_ROWS) -> dict:
        """在工作区执行任意 SQL（沙箱：含 DDL/DML/建视图）。返回 columns/rows。"""
        con = self._connect(workspace)
        try:
            cur = con.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            if not columns:
                return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
            fetched = cur.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [[_jsonable(v) for v in r] for r in fetched[:max_rows]]
            return {"columns": columns, "rows": rows, "row_count": len(rows),
                    "truncated": truncated}
        finally:
            con.close()

    def drop_dataset(self, workspace: str, dataset: str) -> None:
        _valid_name(dataset, "数据集")
        con = self._connect(workspace)
        try:
            con.execute(f'DROP TABLE IF EXISTS "{dataset}"')
            con.execute(f'DROP VIEW IF EXISTS "{dataset}"')
        finally:
            con.close()

    def get_ddl(self, workspace: str, dataset: str) -> str:
        con = self._connect(workspace)
        try:
            row = con.execute(
                "SELECT sql FROM duckdb_tables() WHERE table_name = ?"
                " UNION ALL SELECT sql FROM duckdb_views() WHERE view_name = ?",
                [dataset, dataset]).fetchone()
            return str(row[0]) if row and row[0] else "-- （无 DDL 信息）"
        finally:
            con.close()

    def describe_dataset(self, workspace: str, dataset: str) -> dict:
        con = self._connect(workspace)
        try:
            rows = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                " WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [dataset]).fetchall()
            if not rows:
                raise AnalysisError(f"数据集 {dataset!r} 不存在")
            return {"table": dataset, "schema": None,
                    "columns": [{"name": r[0], "type": r[1],
                                 "nullable": r[2] == "YES", "default": None, "comment": None}
                                for r in rows],
                    "indexes": [], "primary_key": []}
        finally:
            con.close()


def _infer_types(columns: list[str], rows: list[list[Any]]) -> list[str]:
    """按前 200 行推断列类型（值是 engines._jsonable 产物：int/float/bool/str/None）。"""
    types = []
    sample = rows[:200]
    for i in range(len(columns)):
        seen_int = seen_float = seen_bool = seen_str = False
        for r in sample:
            v = r[i] if i < len(r) else None
            if v is None:
                continue
            if isinstance(v, bool):
                seen_bool = True
            elif isinstance(v, int):
                seen_int = True
            elif isinstance(v, float):
                seen_float = True
            else:
                seen_str = True
        if seen_str:
            types.append("VARCHAR")
        elif seen_float:
            types.append("DOUBLE")
        elif seen_int:
            types.append("BIGINT")
        elif seen_bool:
            types.append("BOOLEAN")
        else:
            types.append("VARCHAR")
    return types


def _coerce_row(row: list[Any], types: list[str]) -> list[Any]:
    """按推断类型温和转换（推断样本外出现的异型值转字符串兜底，避免整批失败）。"""
    out = []
    for v, t in zip(row, types, strict=False):
        if v is None:
            out.append(None)
        elif t == "VARCHAR" and not isinstance(v, str):
            out.append(str(v))
        elif t in ("BIGINT", "DOUBLE") and isinstance(v, str):
            out.append(None if v == "" else v)  # duckdb 会尝试转换，失败抛错即报给用户
        elif isinstance(v, dict):  # bytes base64 包装等复杂值
            out.append(str(v))
        else:
            out.append(v)
    return out


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)  # Decimal/date/datetime/bytes 等转字符串
