"""系统设置：后台使用者的界面偏好（主题、Redis 分页/键加载上限），存 dbm.sqlite3。

面向已认证的后台使用者（非 agent），服务端持久化 → 跨浏览器一致。
与审计/审批/片段共用同一个 dbm.sqlite3 文件、各自独立表。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# 已知设置项及默认值。get_all 始终返回全部键（缺失回落默认），前端无需兜底。
DEFAULTS: dict[str, object] = {
    "theme": "dark",           # 界面主题：dark（深色）/ light（浅色），作用于查询台与 Redis 控制台
    "redis_page_size": 100,    # Redis 结果每页行数（键详情集合 / 命令结果）
    "redis_key_limit": 1000,   # Redis 键列表默认加载上限（SCAN）
}

_INT_BOUNDS = {  # 整型设置项的合法区间（保存时夹取）
    "redis_page_size": (10, 2000),
    "redis_key_limit": (100, 100_000),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SettingsStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get_all(self) -> dict:
        """返回全部设置（存储值覆盖默认；类型按默认值还原）。"""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_setting").fetchall()
        stored = {r["key"]: r["value"] for r in rows}
        out = dict(DEFAULTS)
        for key, default in DEFAULTS.items():
            if key in stored:
                out[key] = _coerce(stored[key], default)
        return out

    def save(self, updates: dict) -> dict:
        """更新给定设置项（忽略未知键；非法值夹取/回退），返回更新后的全量设置。"""
        clean: dict[str, str] = {}
        for key, raw in (updates or {}).items():
            if key not in DEFAULTS:
                continue
            clean[key] = _validate(key, raw)
        with self._lock:
            for key, value in clean.items():
                self._conn.execute(
                    "INSERT INTO app_setting (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            self._conn.commit()
        return self.get_all()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _coerce(value: str, default: object) -> object:
    if isinstance(default, bool):
        return value == "true"
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            return default
    return value


def _validate(key: str, raw: object) -> str:
    default = DEFAULTS[key]
    if key == "theme":
        return "light" if str(raw) == "light" else "dark"
    if isinstance(default, int):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = int(default)  # type: ignore[arg-type]
        lo, hi = _INT_BOUNDS.get(key, (None, None))
        if lo is not None:
            n = max(lo, min(hi, n))
        return str(n)
    return str(raw)
