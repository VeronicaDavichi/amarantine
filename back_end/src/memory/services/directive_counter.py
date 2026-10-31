"""B3 滥用护栏的 24h 计数（持久化）。

为什么必须落盘：护栏的意义是「防止无限 pin」。若计数只在内存里，
进程一重启就清零，等于没有护栏——审计时也拿不出证据。

存储选型：SQLite 单表 + 索引，够 P0 用；D1 的 Admin HTTP 复用同一个文件。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory.config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS directive_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT    NOT NULL,
    used_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directive_ns_time
    ON directive_usage (namespace, used_at);
"""


def db_path_from_settings(settings: Settings) -> Path:
    """从 `DATABASE_URL` 取 sqlite 文件路径；非 sqlite 时退回默认路径。"""
    url = settings.DATABASE_URL
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix) :])
    return Path("./data/memory.db")


class DirectiveCounter:
    """记录并统计某 namespace 在 24h 内的显式「记住」次数。"""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def count_pinned(self, namespace: str, *, now: datetime) -> int:
        """返回过去 24h 内该 namespace 已记录的 pin 次数。"""
        window_start = (now - timedelta(hours=24)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM directive_usage"
                " WHERE namespace = ? AND used_at >= ?",
                (namespace, window_start),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_pinned(self, namespace: str, *, now: datetime) -> None:
        """记录一次 pin，参与后续 24h 计数。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO directive_usage (namespace, used_at) VALUES (?, ?)",
                (namespace, now.isoformat()),
            )

    # ---------- 测试与排障 ----------

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM directive_usage")


def utc_now() -> datetime:
    """统一用 UTC 记账，避免本地时区切换导致 24h 窗口漂移。"""
    return datetime.now(timezone.utc)
