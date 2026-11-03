"""「这人姓种」这类姓氏声明的解析与持久化。

## 为什么要它

`_LEADING_MEASURES` 里有 `个`/`位`/`种` 三个单字量词。拦它们的理由很硬：
`小明就是个老实人` 会被正则捕获成 `个老实人`，那是半截短语。

但 `种` 同时是**罕见姓**（`种师道`，读 chóng）。这类「量词兼姓氏」的
字没法靠字本身区分，只能靠额外信息。两个来源：

1. **jieba 词典**：`种师道` 整串就是 `nr` 词条 —— 免费，见
   `labeler._is_person_name_token`。
2. **用户声明**：`这人姓种`。jieba 未必认识生僻姓，但用户说了。

这个模块负责第 2 条的**持久化**。解析（正则）在
`labeler.parse_surname_declarations` —— 那是文本层的事，跟别名抽取放一起。
声明落 SQLite，启动时灌回 `labeler._DECLARED_SURNAMES`。

## 为什么只认「句末/标点前」的 `姓X`

`姓` 是高频构词字：`姓名` / `姓氏` / `老百姓` / `百家姓`。
宽松匹配会把 `姓名` 的 `名` 当成姓。规则和理由见
`labeler.SURNAME_DECL_RE` 的注释。
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

logger = logging.getLogger("memory.surname_decl")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surname_decl (
    namespace   TEXT NOT NULL,
    char        TEXT NOT NULL,
    evidence    TEXT,
    event_id    TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    PRIMARY KEY (namespace, char)
);
CREATE INDEX IF NOT EXISTS idx_surname_decl_active
    ON surname_decl (namespace, revoked_at);
"""


def parse_surname_declarations(text: str) -> list[str]:
    """从原话里抽姓氏声明（转发到 `labeler`，保持单一实现）。"""
    from memory.services.labeler import parse_surname_declarations as _parse

    return _parse(text)


@dataclass
class SurnameDecl:
    namespace: str
    char: str
    evidence: str | None
    event_id: str | None
    created_at: str
    revoked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "char": self.char,
            "evidence": self.evidence,
            "event_id": self.event_id,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }


class SurnameRegistry:
    """姓氏声明表（SQLite）。"""

    def __init__(self, db_path: Path | str, *, clock: Any | None = None) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure()

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

    def declare(
        self,
        namespace: str,
        chars: list[str],
        *,
        evidence: str | None = None,
        event_id: str | None = None,
    ) -> int:
        """登记姓氏。重复登记只刷新证据，不重复计数。"""
        if not chars:
            return 0
        now = self._now()
        fresh = 0
        with self._lock, self._connect() as conn:
            for ch in chars:
                if len(ch) != 1:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM surname_decl WHERE namespace = ? AND char = ?",
                    (namespace, ch),
                ).fetchone()
                conn.execute(
                    "INSERT INTO surname_decl"
                    " (namespace, char, evidence, event_id, created_at, revoked_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT(namespace, char) DO UPDATE SET"
                    "  evidence = excluded.evidence,"
                    "  event_id = excluded.event_id,"
                    "  revoked_at = NULL",
                    (namespace, ch, evidence, event_id, now),
                )
                if exists is None:
                    fresh += 1
        return fresh

    def revoke(self, namespace: str, char: str) -> bool:
        """撤销一条声明（判据用错了，或者用户改口）。"""
        now = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE surname_decl SET revoked_at = ?"
                " WHERE namespace = ? AND char = ? AND revoked_at IS NULL",
                (now, namespace, char),
            )
        return cur.rowcount > 0

    def list_active(self, namespace: str | None = None) -> list[SurnameDecl]:
        sql = "SELECT * FROM surname_decl WHERE revoked_at IS NULL"
        params: list[Any] = []
        if namespace:
            sql += " AND namespace = ?"
            params.append(namespace)
        sql += " ORDER BY namespace ASC, created_at ASC, char ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> SurnameDecl:
        return SurnameDecl(
            namespace=row["namespace"],
            char=row["char"],
            evidence=row["evidence"],
            event_id=row["event_id"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )


def registry_from_settings(
    settings: Settings, *, clock: Any | None = None
) -> SurnameRegistry:
    return SurnameRegistry(db_path_from_settings(settings), clock=clock)


def load_into_labeler(
    settings: Settings, *, registry: SurnameRegistry | None = None
) -> int:
    """把表里的姓氏灌进 `labeler._DECLARED_SURNAMES`。返回灌了几个。

    **不带 namespace 过滤**：`labeler` 是纯函数、没有 namespace 概念
    （跟 jieba 词典一样是进程级状态）。代价是跨 namespace 可见 ——
    对个人记忆代理可接受，且姓氏本身不敏感。
    """
    from memory.services import labeler

    try:
        reg = registry or registry_from_settings(settings)
        chars = [d.char for d in reg.list_active()]
    except Exception as exc:  # noqa: BLE001 - 启动期，读不到就用空集
        logger.warning("加载姓氏声明失败（已忽略）: %s", exc)
        return 0
    added = labeler.register_surnames(chars)
    if added:
        logger.info("加载姓氏声明 %d 个: %s", added, "".join(sorted(chars)))
    return added
