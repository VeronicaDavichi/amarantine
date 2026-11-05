"""E2 引用回路：recall 的 trace 存根 + feedback 幂等落库。

## 为什么要有 trace 存根

`memory_feedback` 的规格里有一条硬约束：

> 只接受该 `trace_id` **实际返回过**的 `event_id`，否则 `isError`

没有存根就没法验证 —— LLM 可能报一个从没被返回过的 id（幻觉，或记错了
trace_id）。所以 `recall` 每次返回都要落一行「这次给了哪些卡」。

## 幂等键为什么是 `(trace_id, event_id)`

同一个 trace 里同一条材料被重复上报是**正常**的（LLM 可能分两轮说
"我用了这些"）。ADR 要求重复上报计入 `deduped` 且 `confirmed_by_use`
**不累加** —— 否则一条材料被夸两次就多算一分，`confirmed_by_use`
会失去「被真正用过几次」的含义。

## `confirmed_by_use` 的唯一写入通道

C.B6 明令禁止自动判定（比如「召回了就当用过」）。这个字段**只能**由
`memory_feedback` 推进，因为只有调用方知道答案到底有没有被引用。

## 为什么和 recall_stats 分表

| 表 | 语义 | 生命周期 |
|---|---|---|
| `recall_stats` | 被**返回**过几次（D7 遗忘用） | 长期累计 |
| `recall_trace` | 某一次召回的**快照** | 短期，可清理 |
| `recall_feedback` | 某一次召回里**真的被用了** | 长期，驱动确认 |

合成一张表会让「返回过」和「用过」混在一起，正好是自动判定想避免的事。
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.recall_count import db_path_from_settings
from memory.services.sqlite_conn import open_db

logger = logging.getLogger("memory.recall_trace")

#: trace_id 前缀，和 `evt_` 风格保持一致
TRACE_PREFIX = "tr_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_trace (
    trace_id     TEXT PRIMARY KEY,
    namespace    TEXT NOT NULL,
    query        TEXT,
    material_ids TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_trace_created
    ON recall_trace (created_at);

CREATE TABLE IF NOT EXISTS recall_feedback (
    trace_id   TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- 幂等键：同 trace 同材料只算一次
    PRIMARY KEY (trace_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_recall_feedback_event
    ON recall_feedback (namespace, event_id);
"""


def new_trace_id() -> str:
    """`tr_` + 12 位十六进制。"""

    return TRACE_PREFIX + secrets.token_hex(6)


@dataclass
class TraceRecord:
    trace_id: str
    namespace: str
    query: str | None
    material_ids: list[str]
    created_at: str


@dataclass
class FeedbackOutcome:
    """一次 feedback 的处理结果。"""

    #: 本次新计入的条数
    accepted: int = 0
    #: 重复上报、已计过，跳过
    deduped: int = 0
    #: `confirmed_by_use` 实际 +1 的材料
    incremented: list[str] = None  # type: ignore[assignment]
    #: 不属于该 trace 的 id（有的话整个请求应当报错）
    rejected: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.incremented is None:
            self.incremented = []
        if self.rejected is None:
            self.rejected = []

    @property
    def ok(self) -> bool:
        return not self.rejected

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "deduped": self.deduped,
            "confirmed_by_use_incremented": list(self.incremented),
        }


class RecallTraceStore:
    """trace 存根 + feedback 幂等表。"""

    def __init__(self, db_path: Path | str, *, clock: Any | None = None) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure()

    # ---------------------------------------------------------------- 内部

    def _connect(self) -> sqlite3.Connection:
        conn = open_db(self._path)  # spec 024：并发参数由 DB_BUSY_TIMEOUT_MS 决定
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    # ---------------------------------------------------------------- trace

    def record_trace(
        self,
        *,
        namespace: str,
        query: str | None,
        material_ids: list[str],
        trace_id: str | None = None,
    ) -> str:
        """记一次召回的返回快照，返回 trace_id。"""
        tid = trace_id or new_trace_id()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO recall_trace"
                " (trace_id, namespace, query, material_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    tid,
                    namespace,
                    query,
                    json.dumps(list(material_ids), ensure_ascii=False),
                    self._now(),
                ),
            )
        return tid

    def get_trace(self, trace_id: str) -> TraceRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recall_trace WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            ids = json.loads(row["material_ids"])
            material_ids = [str(x) for x in ids] if isinstance(ids, list) else []
        except (json.JSONDecodeError, TypeError):
            material_ids = []
        return TraceRecord(
            trace_id=row["trace_id"],
            namespace=row["namespace"],
            query=row["query"],
            material_ids=material_ids,
            created_at=row["created_at"],
        )

    # ---------------------------------------------------------------- feedback

    def has_feedback(self, trace_id: str, event_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM recall_feedback"
                " WHERE trace_id = ? AND event_id = ?",
                (trace_id, event_id),
            ).fetchone()
        return row is not None

    def add_feedback(
        self, *, trace_id: str, namespace: str, event_id: str
    ) -> bool:
        """记一条 feedback。返回 True 表示新插入，False 表示已存在。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO recall_feedback"
                " (trace_id, namespace, event_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (trace_id, namespace, event_id, self._now()),
            )
        return cur.rowcount > 0

    def count_feedback(self, namespace: str, event_id: str) -> int:
        """某条材料累计被确认引用了几次。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM recall_feedback"
                " WHERE namespace = ? AND event_id = ?",
                (namespace, event_id),
            ).fetchone()
        return int(row["n"]) if row else 0

    def prune_traces(self, *, keep_days: int = 7) -> int:
        """清掉太老的 trace 存根。feedback 表不动（那是长期数据）。"""
        cutoff = self._clock().timestamp() - keep_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM recall_trace WHERE created_at < ?", (cutoff_iso,)
            )
        return cur.rowcount


def trace_store_from_settings(
    settings: Settings, *, clock: Any | None = None
) -> RecallTraceStore:
    """按配置造一个 trace 存储（路径与其他派生表同口径）。"""
    return RecallTraceStore(db_path_from_settings(settings), clock=clock)
