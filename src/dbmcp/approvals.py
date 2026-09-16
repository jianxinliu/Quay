"""审批单存储与生命周期。

拒绝—重提 + change_id 放行模式（DESIGN.md 第六节）：
- 写操作首次提交 → 生成 pending 审批单（存储完整 SQL + 风险报告）并拒绝 agent；
- 人在后台 approve / reject；
- agent 带 change_id 重提 → 校验后执行**审批单里存储的 SQL**（重提文本只作指纹校验）；
- 审批单一次性核销（consumed），TTL 60 分钟，过期作废。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_TTL_MINUTES = 60

# 状态机：pending → approved → consumed
#                  ↘ rejected
#         (approved/pending 超时 → expired，惰性判定)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CONSUMED = "consumed"
STATUS_EXPIRED = "expired"

# 审批单种类：sql = 一条/一批 SQL 原文；sync = 跨连接表同步计划（执行的是计划而非 SQL 文本）
KIND_SQL = "sql"
KIND_SYNC = "sync"

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
    database     TEXT,                -- 仅 PG：执行所在的 database（空=连接绑定的库）
    kind         TEXT NOT NULL DEFAULT 'sql',  -- sql（单/多语句）| sync（表同步计划）
    payload      TEXT,                -- JSON：kind=sync 时存 SyncSpec + 建表语句
    reason       TEXT,
    rollback_note TEXT,               -- agent 提交时写的「改动前是什么/怎么回滚」备注
    risk_level   TEXT,
    risk_report  TEXT,                -- JSON
    agent        TEXT,
    session_id   TEXT,
    status       TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    decision_note TEXT,
    exec_result  TEXT                 -- JSON：核销执行后的结果（行数/耗时/执行方）
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
    # 回滚参考：agent 提交写操作时记下的「改动前的数值 / 怎么回滚」，
    # 审批人在审批页看得到，事后 agent 也能用 session_history 取回来据以回滚。
    rollback_note: str = ""
    kind: str = KIND_SQL
    payload: dict | None = None  # kind=sync 的结构化计划（SyncSpec + ddl_sql + columns）
    # 仅 PG：SQL 在哪个 database 上执行。与 SQL 一样是「批的是什么就执行什么」的一部分——
    # 在 A 库批准的 DELETE 不能拿到 B 库去跑。空串 = 连接绑定的库。
    database: str = ""
    decided_by: str = ""
    decided_at: str = ""
    decision_note: str = ""
    exec_result: dict | None = None  # 执行结果（affected_rows/duration_ms/executed_by），未执行为 None

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
            # 老库迁移：exec_result（执行结果回填给等待中的 agent 与后台详情页）、
            # kind/payload（表同步计划型审批单）都是后加的列
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(change_request)")}
            if "exec_result" not in cols:
                self._conn.execute("ALTER TABLE change_request ADD COLUMN exec_result TEXT")
            if "kind" not in cols:
                self._conn.execute(
                    f"ALTER TABLE change_request ADD COLUMN kind TEXT NOT NULL DEFAULT '{KIND_SQL}'")
            if "payload" not in cols:
                self._conn.execute("ALTER TABLE change_request ADD COLUMN payload TEXT")
            if "rollback_note" not in cols:
                self._conn.execute("ALTER TABLE change_request ADD COLUMN rollback_note TEXT")
            if "database" not in cols:
                self._conn.execute("ALTER TABLE change_request ADD COLUMN database TEXT")
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
        rollback_note: str = "",
        kind: str = KIND_SQL,
        payload: dict | None = None,
        database: str | None = None,
    ) -> ChangeRequest:
        now = datetime.now(UTC)
        expires = now + self._ttl
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO change_request
                   (created_at, expires_at, project, connection, environment, engine,
                    sql, fingerprint, reason, risk_level, risk_report, agent, session_id, status,
                    rollback_note, kind, payload, database)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    rollback_note,
                    kind,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    database or None,
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

    def get_many(self, change_ids) -> dict[int, ChangeRequest]:
        """批量取审批单（供审计记录回溯时一次拿齐关联的审批单，避免逐条查库）。

        不存在的 id 直接不出现在结果里（调用方按缺失处理，不报错）。
        """
        ids = sorted({int(i) for i in change_ids if i})
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM change_request WHERE id IN ({marks})", ids
            ).fetchall()
        return {int(r["id"]): self._row_to_change(r) for r in rows}

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

    def consume(self, change_id: int, resubmit_fingerprint: str, connection_key: tuple[str, str],
                database: str | None = None) -> ChangeRequest:
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
        # 重提时没带库名就按审批单里记的库执行；带了但对不上，说明 agent 以为自己在
        # 另一个库上——宁可拒绝，也不在它没想到的库里落地。
        if database is not None and (database or "") != change.database:
            raise ApprovalError(
                f"审批单 #{change_id} 批准的是在库 {change.database or '（连接默认库）'} 上执行，"
                f"本次重提指定的是 {database or '（连接默认库）'}，拒绝执行"
            )
        if change.fingerprint != resubmit_fingerprint:
            what = "同步计划" if change.kind == KIND_SYNC else "SQL"
            raise ApprovalError(
                f"重提的{what}与审批单 #{change_id} 已批准的{what}不一致，拒绝执行。"
                f"请提交与审批时完全相同的{what}，或重新发起审批。"
            )
        # 原子核销（compare-and-swap）：只有 status 仍为 approved 的那一次 UPDATE 生效，
        # rowcount!=1 说明被并发抢先核销——防止两个线程都通过上面的检查后重复执行（双花）。
        # 上面的状态/指纹/连接检查仍保留：给出清晰错误信息 + 惰性过期判定（过期在 DB 里
        # 仍是 approved，故过期拦截必须在 CAS 之前）。
        with self._lock:
            cur = self._conn.execute(
                "UPDATE change_request SET status = ? WHERE id = ? AND status = ?",
                (STATUS_CONSUMED, change_id, STATUS_APPROVED),
            )
            self._conn.commit()
        if cur.rowcount != 1:
            raise ApprovalError(
                f"审批单 #{change_id} 已被使用过（并发核销），一张审批单只能执行一次"
            )
        return self.get(change_id)

    def record_execution(self, change_id: int, result: dict) -> None:
        """回填核销后的执行结果。

        等待中的 agent（wait_for_change）靠它知道「后台按了『批准并执行』，变更已落地、
        行数是多少」，无需再核销一次；后台详情页也据此展示结果。纯记录，不改状态。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE change_request SET exec_result = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), change_id),
            )
            self._conn.commit()

    def purge_old(self, retention_days: int) -> int:
        """删除超过保留期的**终态**审批单（consumed/rejected/expired 及已过期的 pending/approved）。

        未过期的 pending/approved 无论多老都保留（虽然 TTL 60 分钟意味着这不会发生）。
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
            rollback_note=row["rollback_note"] or "",
            kind=row["kind"] or KIND_SQL,
            payload=json.loads(row["payload"]) if row["payload"] else None,
            database=row["database"] or "",
            decided_by=row["decided_by"] or "",
            decided_at=row["decided_at"] or "",
            decision_note=row["decision_note"] or "",
            exec_result=json.loads(row["exec_result"]) if row["exec_result"] else None,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
