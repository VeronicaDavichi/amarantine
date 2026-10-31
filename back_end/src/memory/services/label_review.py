"""B5 打标复核队列（LABEL_REVIEW）。

## 为什么需要它

`types` 参与 `event_id` 的哈希（D1）。打标一抖，**同一件事会被写成两张卡**，
而且两张都"合法"—— 事后从数据上看不出哪个错了。所以规则层和 LLM 层不一致时
不能自己拍板，得有人看。

## 和 NEED_REVIEW 的区别（A24）

**两条独立队列，永不合并**：

| 队列 | 回答的问题 | 出处 |
|---|---|---|
| `NEED_REVIEW` | 这张卡该不该**留**（遗忘） | D7 |
| `LABEL_REVIEW` | 这张卡**打得对不对**（打标） | D9 |

混在一起会导致「一条既想删又打错了的卡」被哪个流程处理掉都说不清。
所以这两个表、两个接口、两个 bucket 参数，各走各的。

## 为什么落 SQLite

复核队列是**人工流程**，量小（只有不一致的才进），不需要向量检索。
放 SQLite 的好处是 `resolve` 能直接用事务表达，不用引入新的状态机。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.recall_count import db_path_from_settings

logger = logging.getLogger("memory.label_review")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS label_review (
    namespace    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    summary      TEXT,
    rules_types  TEXT NOT NULL,
    llm_types    TEXT NOT NULL,
    agreement    REAL NOT NULL,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    resolution   TEXT,
    PRIMARY KEY (namespace, event_id)
);
CREATE INDEX IF NOT EXISTS idx_label_review_pending
    ON label_review (namespace, resolved_at);
"""


@dataclass
class ReviewItem:
    """一条待复核的打标分歧。"""

    namespace: str
    event_id: str
    summary: str | None
    rules_types: list[str]
    llm_types: list[str]
    agreement: float
    created_at: str
    resolved_at: str | None = None
    resolution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "summary": self.summary,
            "rules_types": list(self.rules_types),
            "llm_types": list(self.llm_types),
            "agreement": round(self.agreement, 3),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
        }


class LabelReviewQueue:
    """打标复核队列（SQLite）。"""

    def __init__(self, db_path: Path | str, *, clock: Any | None = None) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure()

    # ---------------------------------------------------------------- 内部

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    # ---------------------------------------------------------------- 写入

    def push(
        self,
        *,
        namespace: str,
        event_id: str,
        summary: str | None,
        rules_types: list[str],
        llm_types: list[str],
        agreement: float,
    ) -> bool:
        """入队。同一张卡重复入队 → 更新为新的一次分歧（幂等）。

        Returns:
            True 表示新建，False 表示覆盖了已有未决记录。
        """
        now = self._now()
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT resolved_at FROM label_review"
                " WHERE namespace = ? AND event_id = ?",
                (namespace, event_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO label_review"
                " (namespace, event_id, summary, rules_types, llm_types,"
                "  agreement, created_at, resolved_at, resolution)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
                " ON CONFLICT(namespace, event_id) DO UPDATE SET"
                "  summary = excluded.summary,"
                "  rules_types = excluded.rules_types,"
                "  llm_types = excluded.llm_types,"
                "  agreement = excluded.agreement,"
                "  created_at = excluded.created_at,"
                "  resolved_at = NULL,"
                "  resolution = NULL",
                (
                    namespace,
                    event_id,
                    summary,
                    json.dumps(list(rules_types), ensure_ascii=False),
                    json.dumps(list(llm_types), ensure_ascii=False),
                    float(agreement),
                    now,
                ),
            )
        return exists is None

    def resolve(
        self, namespace: str, event_id: str, *, resolution: str = "accepted"
    ) -> bool:
        """标记已处理。返回 False 表示这条记录不存在。"""
        now = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE label_review SET resolved_at = ?, resolution = ?"
                " WHERE namespace = ? AND event_id = ? AND resolved_at IS NULL",
                (now, resolution, namespace, event_id),
            )
        return cur.rowcount > 0

    # ---------------------------------------------------------------- 读取

    def list_pending(
        self, namespace: str, *, limit: int = 50
    ) -> list[ReviewItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM label_review"
                " WHERE namespace = ? AND resolved_at IS NULL"
                " ORDER BY created_at ASC, event_id ASC LIMIT ?",
                (namespace, int(limit)),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def count_pending(self, namespace: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM label_review"
                " WHERE namespace = ? AND resolved_at IS NULL",
                (namespace,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def get(self, namespace: str, event_id: str) -> ReviewItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM label_review WHERE namespace = ? AND event_id = ?",
                (namespace, event_id),
            ).fetchone()
        return self._row_to_item(row) if row else None

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ReviewItem:
        def _loads(raw: str) -> list[str]:
            try:
                val = json.loads(raw)
                return [str(x) for x in val] if isinstance(val, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        return ReviewItem(
            namespace=row["namespace"],
            event_id=row["event_id"],
            summary=row["summary"],
            rules_types=_loads(row["rules_types"]),
            llm_types=_loads(row["llm_types"]),
            agreement=float(row["agreement"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
        )


def queue_from_settings(
    settings: Settings, *, clock: Any | None = None
) -> LabelReviewQueue:
    """按配置造一个队列（路径与 recall_count 同口径）。"""
    return LabelReviewQueue(db_path_from_settings(settings), clock=clock)
