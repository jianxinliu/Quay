"""审批单存储与生命周期。

拒绝—重提 + change_id 放行模式（DESIGN.md 第六节）：
- 写操作首次提交 → 生成 pending 审批单（存储完整 SQL + 风险报告）并拒绝 agent；
- 人在后台 approve / reject；
- agent 带 change_id 重提 → 校验后执行**审批单里存储的 SQL**（重提文本只作指纹校验）；
- 审批单一次性核销（consumed），TTL 30 分钟，过期作废。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_TTL_MINUTES = 30

# 状态机：pending → approved → consumed
#                  ↘ rejected
#         (approved/pending 超时 → expired，惰性判定)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CONSUMED = "consumed"
STATUS_EXPIRED = "expired"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS change_request (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    project      TEXT NOT NULL,
    connection   TEXT NOT NULL,
    environment  TEXT,
    engine       TEXT,
    sql          TEXT NOT NULL,       -- 审批与执行的唯一真实来源
    fingerprint  TEXT NOT NULL,
    reason       TEXT,
    risk_level   TEXT,
    risk_report  TEXT,                -- JSON
    agent        TEXT,
    session_id   TEXT,
    status       TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    decision_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_status ON change_request (status);
"""


@dataclass
class ChangeRequest:
    id: int
    created_at: str
    expires_at: str
    project: str
    connection: str
    environment: str
    engine: str
    sql: str
    fingerprint: str
    reason: str
    risk_level: str
    risk_report: dict
    agent: str
    session_id: str
    status: str
    decided_by: str = ""
    decided_at: str = ""
    decision_note: str = ""

    def effective_status(self, now: datetime | None = None) -> str:
        """惰性过期判定：pending/approved 超过 expires_at 视为 expired。"""
        if self.status in (STATUS_CONSUMED, STATUS_REJECTED, STATUS_EXPIRED):
            return self.status
        now = now or datetime.now(UTC)
        if datetime.fromisoformat(self.expires_at) < now:
            return STATUS_EXPIRED
        return self.status


class ApprovalError(Exception):
    """审批单状态非法（不存在 / 已核销 / 已过期 / 连接不匹配 / SQL 不一致等）。"""


class ApprovalStore:
    def __init__(self, db_path: str | Path, ttl_minutes: int = DEFAULT_TTL_MINUTES):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._ttl = timedelta(minutes=ttl_minutes)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create(
        self,
        *,
        project: str,
        connection: str,
        environment: str,
        engine: str,
        sql: str,
        fingerprint: str,
        reason: str,
        risk_level: str,
        risk_report: dict,
        agent: str,
        session_id: str,
    ) -> ChangeRequest:
        now = datetime.now(UTC)
        expires = now + self._ttl
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO change_request
                   (created_at, expires_at, project, connection, environment, engine,
                    sql, fingerprint, reason, risk_level, risk_report, agent, session_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                    project,
                    connection,
                    environment,
                    engine,
                    sql,
                    fingerprint,
                    reason,
                    risk_level,
                    json.dumps(risk_report, ensure_ascii=False),
                    agent,
                    session_id,
                    STATUS_PENDING,
                ),
            )
            self._conn.commit()
            change_id = int(cur.lastrowid or 0)
        return self.get(change_id)

    def get(self, change_id: int) -> ChangeRequest:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM change_request WHERE id = ?", (change_id,)
            ).fetchone()
        if row is None:
            raise ApprovalError(f"审批单 #{change_id} 不存在")
        return self._row_to_change(row)

    def list_by_status(self, status: str | None = None, limit: int = 200) -> list[ChangeRequest]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM change_request WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM change_request ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_change(r) for r in rows]

    def approve(self, change_id: int, decided_by: str, note: str = "") -> ChangeRequest:
        return self._decide(change_id, STATUS_APPROVED, decided_by, note)

    def reject(self, change_id: int, decided_by: str, note: str = "") -> ChangeRequest:
        return self._decide(change_id, STATUS_REJECTED, decided_by, note)

    def _decide(self, change_id: int, new_status: str, decided_by: str, note: str) -> ChangeRequest:
        change = self.get(change_id)
        effective = change.effective_status()
        if effective != STATUS_PENDING:
            raise ApprovalError(f"审批单 #{change_id} 当前状态为 {effective}，无法再决策")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE change_request SET status = ?, decided_by = ?, decided_at = ?,"
                " decision_note = ? WHERE id = ?",
                (new_status, decided_by, now, note, change_id),
            )
            self._conn.commit()
        return self.get(change_id)

    def consume(self, change_id: int, resubmit_fingerprint: str, connection_key: tuple[str, str]) -> ChangeRequest:
        """校验并核销一张已批准的审批单，返回其存储的 SQL 供执行。

        校验：状态 approved 且未过期；project/connection 匹配；重提 SQL 指纹与审批单一致。
        校验通过即置为 consumed（一次性），执行的是审批单里存储的 SQL。
        """
        change = self.get(change_id)
        effective = change.effective_status()
        if effective == STATUS_EXPIRED:
            raise ApprovalError(f"审批单 #{change_id} 已过期（TTL {self._ttl}），请重新提交")
        if effective == STATUS_CONSUMED:
            raise ApprovalError(f"审批单 #{change_id} 已被使用过，一张审批单只能执行一次")
        if effective == STATUS_REJECTED:
            note = f"：{change.decision_note}" if change.decision_note else ""
            raise ApprovalError(f"审批单 #{change_id} 已被拒绝{note}，请按意见调整后重新提交")
        if effective == STATUS_PENDING:
            raise ApprovalError(f"审批单 #{change_id} 尚未审批，请等待人工审批后再重提")
        if (change.project, change.connection) != connection_key:
            raise ApprovalError(
                f"审批单 #{change_id} 属于连接 {change.project}/{change.connection}，与本次提交不符"
            )
        if change.fingerprint != resubmit_fingerprint:
            raise ApprovalError(
                f"重提的 SQL 与审批单 #{change_id} 已批准的 SQL 不一致，拒绝执行。"
                "请提交与审批时完全相同的 SQL，或重新发起审批。"
            )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE change_request SET status = ? WHERE id = ?", (STATUS_CONSUMED, change_id)
            )
            self._conn.commit()
        return self.get(change_id)

    def purge_old(self, retention_days: int) -> int:
        """删除超过保留期的**终态**审批单（consumed/rejected/expired 及已过期的 pending/approved）。

        未过期的 pending/approved 无论多老都保留（虽然 TTL 30 分钟意味着这不会发生）。
        """
        now = datetime.now(UTC)
        cutoff = (now - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM change_request WHERE created_at < ?"
                " AND (status IN (?, ?) OR expires_at < ?)",
                (cutoff, STATUS_CONSUMED, STATUS_REJECTED, now.isoformat(timespec="seconds")),
            )
            self._conn.commit()
        return cur.rowcount or 0

    def _row_to_change(self, row: sqlite3.Row) -> ChangeRequest:
        return ChangeRequest(
            id=row["id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            project=row["project"],
            connection=row["connection"],
            environment=row["environment"] or "",
            engine=row["engine"] or "",
            sql=row["sql"],
            fingerprint=row["fingerprint"],
            reason=row["reason"] or "",
            risk_level=row["risk_level"] or "",
            risk_report=json.loads(row["risk_report"]) if row["risk_report"] else {},
            agent=row["agent"] or "",
            session_id=row["session_id"] or "",
            status=row["status"],
            decided_by=row["decided_by"] or "",
            decided_at=row["decided_at"] or "",
            decision_note=row["decision_note"] or "",
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
