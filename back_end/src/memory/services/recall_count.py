"""D7 召回计数回写（A13 / A19）。

两条口径，写错任何一条都会让遗忘策略失效：

- **A13**：只有进了 rerank 后的 top-K 返回集才 +1，进候选池不算。
- **A19**：`score < RECALL_COUNT_MIN_SCORE` 的即使被返回也**不** +1。

为什么要 A19：动态 K 会让低分条目也进返回集。若沿用「进 top-K 就 +1」，
垃圾条目会因为被大量召回而获得留存豁免 —— 正好破坏遗忘的初衷。

落点选择：**独立 SQLite 表**而非 Weaviate 字段。
理由：计数是派生数据，丢了能重建；而每次召回都去 UPDATE Weaviate
会造成写放大，还会和向量库的 segment 合并抢 IO。

写模式：**批量 flush**（ADR：60s 或 N 次），避免每次调用一次事务。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory.config import Settings

from memory.services.sqlite_conn import open_db

logger = logging.getLogger("memory.recall_count")

#: 攒够多少条就落库
FLUSH_EVERY = 20
#: 攒多久就落库（秒）
FLUSH_INTERVAL_S = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_stats (
    namespace        TEXT    NOT NULL,
    event_id         TEXT    NOT NULL,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT    NOT NULL,
    PRIMARY KEY (namespace, event_id)
);
"""


def db_path_from_settings(settings: Settings) -> Path:
    """从 DATABASE_URL 推出 SQLite 文件路径（与 directive_counter 同口径）。"""
    url = settings.DATABASE_URL
    if not url.startswith("sqlite"):
        raise ValueError(f"recall_count 目前只支持 SQLite：{url}")
    raw = url.split("///", 1)[-1]
    return Path(raw).expanduser().resolve()


@dataclass
class _Pending:
    namespace: str
    event_id: str
    at: str


class RecallCounter:
    """内存累积 + 定期落库的召回计数器。

    线程安全：内部用一把锁保护缓冲区和 flush，可以在扇出的 worker 里放心调 `record`。
    """

    def __init__(
        self,
        db_path: Path,
        *,
        flush_every: int = FLUSH_EVERY,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        clock=time.monotonic,
    ) -> None:
        self._db_path = Path(db_path)
        self._flush_every = max(1, flush_every)
        self._flush_interval_s = max(0.0, flush_interval_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[_Pending] = []
        self._last_flush = clock()
        self._ensure_schema()

    # ---------- 建表 ----------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(self._db_path)  # spec 024：并发参数由 DB_BUSY_TIMEOUT_MS 决定
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 写入 ----------

    def record(
        self,
        namespace: str,
        event_id: str,
        *,
        score: float,
        min_score: float,
        now: datetime | None = None,
    ) -> bool:
        """记录一次召回。返回是否真的计数（分数不够则丢弃）。

        Args:
            namespace: 记忆域。
            event_id: 事件 id。
            score: 该条的打分。
            min_score: 计入门槛（`RECALL_COUNT_MIN_SCORE`）。
            now: 调用时刻，缺省 UTC。
        """
        if score < min_score:
            # A19：低分命中不算「被用过」
            return False

        moment = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._pending.append(_Pending(namespace=namespace, event_id=event_id, at=moment))
            should_flush = (
                len(self._pending) >= self._flush_every
                or (self._clock() - self._last_flush) >= self._flush_interval_s
            )
        if should_flush:
            self.flush()
        return True

    def record_many(
        self,
        namespace: str,
        items: list[tuple[str, float]],
        *,
        min_score: float,
        now: datetime | None = None,
    ) -> int:
        """批量记录，返回实际计数条数。"""
        counted = 0
        for event_id, score in items:
            if self.record(
                namespace, event_id, score=score, min_score=min_score, now=now
            ):
                counted += 1
        return counted

    # ---------- 落库 ----------

    def flush(self) -> int:
        """把缓冲区写入 SQLite，返回写入条数。"""
        with self._lock:
            if not self._pending:
                self._last_flush = self._clock()
                return 0
            batch = self._pending
            self._pending = []
            self._last_flush = self._clock()

        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO recall_stats (namespace, event_id, recall_count, last_recalled_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(namespace, event_id) DO UPDATE SET
                        recall_count = recall_count + 1,
                        last_recalled_at = excluded.last_recalled_at
                    """,
                    [(p.namespace, p.event_id, p.at) for p in batch],
                )
            return len(batch)
        except sqlite3.Error as exc:
            # 计数是派生数据，失败不能阻塞召回返回
            logger.warning("召回计数落库失败（%d 条丢弃）: %s", len(batch), exc)
            return 0

    # ---------- 查询（测试与 admin 用） ----------

    def stats(self, namespace: str, event_id: str) -> tuple[int, str | None]:
        """返回 (recall_count, last_recalled_at)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recall_count, last_recalled_at FROM recall_stats "
                "WHERE namespace = ? AND event_id = ?",
                (namespace, event_id),
            ).fetchone()
        return (int(row[0]), row[1]) if row else (0, None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending = []
        with self._connect() as conn:
            conn.execute("DELETE FROM recall_stats")
