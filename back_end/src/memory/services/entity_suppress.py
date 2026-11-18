# -*- coding: utf-8 -*-
"""实体抑制表：被判定为「不是实体」的名字，写入时直接跳过（spec 044 T1/T3）。

## 为什么要它

人名碎片（`Veronica` ⊂ `Veronica Davichi`、`瓦一` ⊂ `新机子瓦一兔子`）
会被当成 `Entity` 建节点，污染检索与关系层。**人工一条条合并不可持续**，
所以改成：判定一次 → 记进抑制表 → 之后写入自动跳过。

## 为什么放 SQLite 而不是图属性

图是派生层，`rebuild_graph` 一跑属性就没了；抑制状态必须活在图的**外面**。

## 缓存

写路径上每次都查库太贵，所以进程内缓存一份 rejected 名字集合；
`add()` 之后主动失效。多进程部署时会有短暂不一致，可接受（下一轮扫描会纠正）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("memory.entity_suppress")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_suppress (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    decided_by  TEXT NOT NULL DEFAULT 'auto',
    status      TEXT NOT NULL DEFAULT 'rejected',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, name)
);
"""

#: `namespace → set(name)` 缓存；None 表示还没读
_CACHE: dict[str, set[str]] = {}
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_path(settings: Any | None = None) -> Path:
    db = getattr(settings, "DB_PATH", None) if settings is not None else None
    if db:
        return Path(str(db))
    return Path(__file__).resolve().parents[3] / "data" / "memory.db"


class EntitySuppressStore:
    """抑制表的读写。线程安全（单锁 + 每次独立连接）。"""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._lock = _LOCK
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _ensure(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def rejected(self, namespace: str) -> set[str]:
        """某 namespace 下被抑制的名字（带进程内缓存）。"""
        with self._lock:
            cached = _CACHE.get(namespace)
            if cached is not None:
                return cached
        with self._connect() as con:
            rows = con.execute(
                "SELECT name FROM entity_suppress WHERE namespace=? AND status='rejected'",
                (namespace,)).fetchall()
        got = {r["name"] for r in rows}
        with self._lock:
            _CACHE[namespace] = got
        return got

    def add(
        self,
        namespace: str,
        names: list[str],
        *,
        reason: str,
        decided_by: str = "auto",
        detail: str = "",
    ) -> int:
        """把名字记成 rejected（幂等）。返回新插入条数。"""
        items = [(namespace, n, reason, decided_by, detail, _now(), _now())
                 for n in names if n]
        if not items:
            return 0
        inserted = 0
        with self._lock, self._connect() as con:
            for row in items:
                cur = con.execute(
                    "INSERT INTO entity_suppress"
                    " (namespace, name, reason, decided_by, status, detail,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'rejected', ?, ?, ?)"
                    " ON CONFLICT(namespace, name) DO UPDATE SET"
                    "   reason=excluded.reason, status='rejected',"
                    "   detail=excluded.detail, updated_at=excluded.updated_at",
                    row,
                )
                inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            _CACHE.pop(namespace, None)
        return inserted

    def revive(self, namespace: str, names: list[str]) -> int:
        """解除抑制（自动复活路径用）。"""
        if not names:
            return 0
        with self._lock, self._connect() as con:
            n = 0
            for name in names:
                cur = con.execute(
                    "UPDATE entity_suppress SET status='active', updated_at=?"
                    " WHERE namespace=? AND name=? AND status='rejected'",
                    (_now(), namespace, name))
                n += cur.rowcount or 0
            _CACHE.pop(namespace, None)
        return n

    def list_all(self, namespace: str, *, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM entity_suppress WHERE namespace=?"
        args: list[Any] = [namespace]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY updated_at DESC"
        with self._connect() as con:
            return [dict(r) for r in con.execute(q, args).fetchall()]


def store_for(settings: Any | None = None) -> EntitySuppressStore:
    return EntitySuppressStore(default_path(settings))


def rejected_names(namespace: str, settings: Any | None = None) -> set[str]:
    """快路径：拿被抑制的名字集合（失败返回空集，绝不阻断写入）。"""
    try:
        return store_for(settings).rejected(namespace)
    except Exception as exc:  # noqa: BLE001 - 抑制是增强，不能拖垮主流程
        logger.warning("读实体抑制表失败（忽略）: %s", exc)
        return set()
