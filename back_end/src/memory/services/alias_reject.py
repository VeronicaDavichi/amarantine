"""别名声明被拒的留痕表。

## 为什么需要它

别名声明从「原话」到「图上一条边」，中间有两道判据：

    原话 ──extract_alias_claims──> claim ──graph_outbox──> ALIAS_OF 边
                │                              │
                └── 左值不像名字就丢            └── 右值不像名字就丢

两道原来都是**布尔过滤**，丢掉之后不留任何痕迹。代价是实测出来的：

- `印度人` 因为标记集合里有「人」被丢，两个会话、查真库才发现；
- `科学家` 因为标记集合里有「家」被丢，同一个错误犯了第二次；
- `种师道` 因为首字是量词被丢，LLM 复核层**根本看不到它**
  （左值被拒时连 claim 都不生成）。

模式很清楚：**判错了不要紧，判错了还看不见才要命。** 这张表把每一次
拒绝连同理由一起落下来，于是「为什么这条边没建」有了答案。

## 为什么落 SQLite

跟 `label_review` 同构，理由也相同：人工/LLM 复核是低量流程，
不需要向量检索，`resolve` 用一个事务就能表达。

## 和 `label_review` 的区别

| 表 | 记什么 | 谁能修 |
|---|---|---|
| `label_review` | 打标分歧（该不该是这个 type） | 人 |
| `alias_reject` | 别名边被拒（该不该建这条边） | 人 / LLM |

`alias_reject.resolution` 留了 `"accepted"` 这个值，意思是「人工看过，
判据错了，这条应该建边」—— 后续可以据此写一条边并下架该条记录。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.recall_count import db_path_from_settings
from memory.services.sqlite_conn import open_db

logger = logging.getLogger("memory.alias_reject")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alias_reject (
    namespace   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    side        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    source_text TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT,
    PRIMARY KEY (namespace, event_id, alias, canonical, side)
);
CREATE INDEX IF NOT EXISTS idx_alias_reject_pending
    ON alias_reject (namespace, resolved_at);
CREATE INDEX IF NOT EXISTS idx_alias_reject_reason
    ON alias_reject (namespace, reason);
"""


@dataclass
class AliasRejectItem:
    """一条被拒的别名声明。"""

    namespace: str
    event_id: str
    alias: str
    canonical: str
    side: str
    reason: str
    detail: str
    source_text: str | None
    created_at: str
    resolved_at: str | None = None
    resolution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "alias": self.alias,
            "canonical": self.canonical,
            "side": self.side,
            "reason": self.reason,
            "detail": self.detail,
            "source_text": self.source_text,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
        }


class AliasRejectQueue:
    """别名拒绝留痕（SQLite）。"""

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

    # ---------------------------------------------------------------- 写入

    def record_many(
        self,
        namespace: str,
        event_id: str,
        rejects: list[dict[str, str]],
        *,
        source_text: str | None = None,
    ) -> int:
        """批量记下被拒的声明。返回**新插入**的条数。

        同一 `(namespace, event_id, alias, canonical, side)` 重复上报 →
        覆盖理由并清掉旧的 `resolution`（判据可能已经改了，
        拿旧的「已处理」挡着会导致新问题看不见）。
        """
        if not rejects:
            return 0
        now = self._now()
        inserted = 0
        with self._lock, self._connect() as conn:
            for item in rejects:
                cur = conn.execute(
                    "INSERT INTO alias_reject"
                    " (namespace, event_id, alias, canonical, side, reason,"
                    "  detail, source_text, created_at, resolved_at, resolution)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
                    " ON CONFLICT(namespace, event_id, alias, canonical, side)"
                    " DO UPDATE SET"
                    "  reason = excluded.reason,"
                    "  detail = excluded.detail,"
                    "  source_text = excluded.source_text,"
                    "  created_at = excluded.created_at,"
                    "  resolved_at = NULL,"
                    "  resolution = NULL",
                    (
                        namespace,
                        event_id,
                        str(item.get("alias", "")),
                        str(item.get("canonical", "")),
                        str(item.get("side", "")),
                        str(item.get("reason", "")),
                        str(item.get("detail", "")),
                        source_text,
                        now,
                    ),
                )
                # `ON CONFLICT DO UPDATE` 在 rowcount 上不可靠地区分
                # 插入/更新，这里用 `last_insert_rowid` 变化判断没意义。
                # 「新插入」按 `changes()` 累计足够近似，且调用方只用它做日志。
                if cur.rowcount > 0:
                    inserted += 1
        return inserted

    def resolve(
        self,
        namespace: str,
        *,
        event_id: str,
        alias: str,
        canonical: str,
        side: str,
        resolution: str = "accepted",
    ) -> bool:
        """标记已处理（人工/LLM 看过）。返回 False 表示这条不存在或已处理。

        `accept_pending` 也算「可再处理」：那是「人已裁决 accepted、但补建
        别名边失败」的中间态。它必须还能被 resolve 重试一次，否则失败就
        永久沉底 —— 人以为修好了，其实没有。
        """
        now = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE alias_reject SET resolved_at = ?, resolution = ?"
                " WHERE namespace = ? AND event_id = ? AND alias = ?"
                "   AND canonical = ? AND side = ?"
                "   AND (resolved_at IS NULL OR resolution = 'accept_pending')",
                (now, resolution, namespace, event_id, alias, canonical, side),
            )
        return cur.rowcount > 0

    # ---------------------------------------------------------------- 读取

    def list_pending(
        self,
        namespace: str,
        *,
        limit: int = 50,
        reason: str | None = None,
    ) -> list[AliasRejectItem]:
        sql = (
            "SELECT * FROM alias_reject"
            " WHERE namespace = ?"
            "   AND (resolved_at IS NULL OR resolution = 'accept_pending')"
        )
        params: list[Any] = [namespace]
        if reason:
            sql += " AND reason = ?"
            params.append(reason)
        sql += " ORDER BY created_at ASC, event_id ASC, alias ASC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    def count_pending(self, namespace: str, *, reason: str | None = None) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM alias_reject"
            " WHERE namespace = ?"
            "   AND (resolved_at IS NULL OR resolution = 'accept_pending')"
        )
        params: list[Any] = [namespace]
        if reason:
            sql += " AND reason = ?"
            params.append(reason)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def stats(self, namespace: str) -> dict[str, int]:
        """按理由统计未处理的拒绝数 —— 判断「判据该不该改」用这个。

        口径与 `count_pending` 一致：`accept_pending`（人已裁决、补边失败）
        也算未处理，否则这类失败会从「判据该不该改」的统计里消失。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT reason, COUNT(*) AS n FROM alias_reject"
                " WHERE namespace = ?"
                "   AND (resolved_at IS NULL OR resolution = 'accept_pending')"
                " GROUP BY reason ORDER BY n DESC",
                (namespace,),
            ).fetchall()
        return {row["reason"]: int(row["n"]) for row in rows}

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> AliasRejectItem:
        return AliasRejectItem(
            namespace=row["namespace"],
            event_id=row["event_id"],
            alias=row["alias"],
            canonical=row["canonical"],
            side=row["side"],
            reason=row["reason"],
            detail=row["detail"],
            source_text=row["source_text"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
        )


def queue_from_settings(
    settings: Settings, *, clock: Any | None = None
) -> AliasRejectQueue:
    """按配置造一个留痕队列（路径与 recall_count 同口径）。"""
    return AliasRejectQueue(db_path_from_settings(settings), clock=clock)


def record_safe(
    settings: Settings,
    *,
    namespace: str,
    event_id: str,
    rejects: list[dict[str, str]],
    source_text: str | None = None,
    queue: AliasRejectQueue | None = None,
) -> int:
    """记一笔拒绝，**失败只告警**。

    留痕是旁路诊断：它挂了绝不能让「记忆本身写不进去」。
    跟 `remember._run_dual_check` 一个态度。
    """
    if not rejects:
        return 0
    try:
        q = queue or queue_from_settings(settings)
        return q.record_many(
            namespace, event_id, rejects, source_text=source_text
        )
    except Exception as exc:  # noqa: BLE001 - 旁路诊断，绝不影响写入
        logger.warning("别名拒绝留痕失败（已忽略）: %s", exc)
        return 0
