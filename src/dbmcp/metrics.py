"""运行指标：当前在跑的操作登记簿 + 结果体积估算。

看板（`/admin/dashboard`）要回答两类问题：

- **此刻**谁在查、查多久了 —— 审计记录是**操作结束后**才落库的，所以「正在执行」这件事
  在 audit_log 里根本不存在，必须另有一份进程内的在途登记簿（`LiveOps`）。
- **一段时间内**传了多少数据 —— 由 audit_log 聚合（见 `AuditStore.traffic_*`），
  其中的字节数来自本模块的 `estimate_result_bytes`。

字节数是**估算**而非精确的网络字节数：真实链路上还有协议帧、压缩、SSH 隧道封装，
拿不到也没必要拿。这里量的是「结果集本身有多大」——按已 `_jsonable` 化、已截断的
单元格值算，与使用者在查询台看到的、agent 拿进上下文的数据量一致，正是要衡量的东西。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# 单个单元格计入体积时的固定开销（分隔符/引号/字段名摊销），避免把大量小整数列算成几乎 0 字节
_CELL_OVERHEAD_BYTES = 2


def estimate_cell_bytes(value: Any) -> int:
    """单个单元格值的估算字节数（UTF-8 计长）。

    值已由 `engines._jsonable` 归一过：bytes 包成 {"__bytes_base64__": …}、
    大整数转字符串、Decimal 转字符串，所以这里只需处理 str / 数字 / 布尔 / None /
    容器四类。容器（JSON 列、bytes 包装）递归求和。
    """
    if value is None:
        return _CELL_OVERHEAD_BYTES
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace")) + _CELL_OVERHEAD_BYTES
    if isinstance(value, bytes):
        return len(value) + _CELL_OVERHEAD_BYTES
    if isinstance(value, bool):
        return 5 + _CELL_OVERHEAD_BYTES
    if isinstance(value, (int, float)):
        return len(repr(value)) + _CELL_OVERHEAD_BYTES
    if isinstance(value, dict):
        return sum(estimate_cell_bytes(k) + estimate_cell_bytes(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(estimate_cell_bytes(v) for v in value)
    return len(str(value).encode("utf-8", "replace")) + _CELL_OVERHEAD_BYTES


def estimate_result_bytes(columns: list[str], rows: list[list[Any]]) -> int:
    """结果集的估算字节数 = 列名一次 + 所有单元格。空结果返回 0。"""
    if not rows and not columns:
        return 0
    total = sum(estimate_cell_bytes(c) for c in columns)
    for row in rows:
        total += sum(estimate_cell_bytes(v) for v in row)
    return total


def human_bytes(n: int | float | None) -> str:
    """字节数转人类可读（看板与 CLI 共用）。"""
    if not n:
        return "0 B"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@dataclass
class _LiveOp:
    op_id: int
    project: str
    connection: str
    tool: str
    agent: str
    session_id: str
    sql: str
    started_at: float          # time.monotonic()
    started_ts: str            # ISO UTC，供前端显示绝对时间


class LiveOps:
    """当前正在执行的 DB 操作登记簿（线程安全）。

    只反映**本进程**的在途操作：daemon 是单进程常驻的，MCP 与管理后台共用同一个
    DbmService，所以这一份就是全部。stdio 模式下另起的进程各有各的登记簿——那本来
    也是独立实例，看板看的是自己这台服务。
    """

    def __init__(self, max_sql_chars: int = 300) -> None:
        self._ops: dict[int, _LiveOp] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._max_sql_chars = max_sql_chars

    def begin(
        self, project: str, connection: str, tool: str,
        agent: str = "", session_id: str = "", sql: str = "",
    ) -> int:
        sql = (sql or "").strip()
        if len(sql) > self._max_sql_chars:
            sql = sql[: self._max_sql_chars] + "…"
        with self._lock:
            op_id = self._next_id
            self._next_id += 1
            self._ops[op_id] = _LiveOp(
                op_id=op_id, project=project, connection=connection, tool=tool,
                agent=agent, session_id=session_id, sql=sql,
                started_at=time.monotonic(),
                started_ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            )
        return op_id

    def end(self, op_id: int) -> None:
        with self._lock:
            self._ops.pop(op_id, None)

    @contextmanager
    def track(
        self, project: str, connection: str, tool: str,
        agent: str = "", session_id: str = "", sql: str = "",
    ) -> Iterator[int]:
        op_id = self.begin(project, connection, tool, agent, session_id, sql)
        try:
            yield op_id
        finally:
            self.end(op_id)

    def snapshot(self) -> list[dict]:
        """在途操作快照，跑得最久的在前（看板最关心「哪条卡住了」）。"""
        now = time.monotonic()
        with self._lock:
            ops = list(self._ops.values())
        out = [
            {
                "op_id": op.op_id,
                "project": op.project,
                "connection": op.connection,
                "tool": op.tool,
                "agent": op.agent,
                "session_id": op.session_id,
                "sql": op.sql,
                "started_ts": op.started_ts,
                "elapsed_ms": max(int((now - op.started_at) * 1000), 0),
            }
            for op in ops
        ]
        out.sort(key=lambda o: o["elapsed_ms"], reverse=True)
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._ops)
