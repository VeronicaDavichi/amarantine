"""A25：换 embedding 模型必须拒绝写入。

**为什么这是硬伤**：不同模型的向量可能**维度相同、空间完全不同**。
把 B 模型的向量写进 A 模型的索引，余弦相似度依然算得出数字 ——
不报错，只给出错误的相似度。

这是最危险的一类故障：**静默的、看起来正常的错误**。
检索质量慢慢烂掉，而所有监控都是绿的。

所以校验必须是**错误**而不是告警（ADR 原文特意写了「非告警」——
因为告警会被忽略），且必须发生在**调用 embedding 服务之前**
（拦住的同时别白花一次编码的钱）。

设计取舍见 `docs/acceptance-audit.md` 与 `specs/012-embedding-guard`。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.raw_quote import db_path_from_settings
from memory.services.sqlite_conn import open_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_index_meta (
    namespace  TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class EmbeddingModelMismatchError(RuntimeError):
    """索引已被另一个模型占用。

    入口层应映射成 **409 Conflict** —— 不是 400（参数本身没错），
    也不是 503（服务没挂）。
    """

    def __init__(self, namespace: str, expected: str, actual: str) -> None:
        self.namespace = namespace
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"namespace {namespace!r} 的索引由 {expected!r} 生成，"
            f"当前 EMBEDDING_MODEL={actual!r}；"
            "换模型必须先重建索引（或把模型改回来）"
        )


class EmbeddingIndexMeta:
    """记录每个 namespace 的索引是哪个模型建的。

    **为什么用 SQLite 而不是 Weaviate class**：

    - 空索引时无从判断该用哪个模型，而 meta 必须在第一条数据进来之前就能读
    - 每次写入多一次网络往返，没必要

    **为什么不给每条数据加 `embedding_model` 字段**：

    本 spec 的前提是「一个 namespace 只服务一个模型」。在这个前提下
    per-row 字段是冗余的（同一个值重复 N 遍）。真要做多模型共存时再加，
    那时本表也要改成 `(namespace, model)` 复合键。
    """

    def __init__(self, db_path: Path | str, *, clock: Any | None = None) -> None:
        self._path = Path(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._ensure_schema()

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(self._path)  # spec 024：并发参数由 DB_BUSY_TIMEOUT_MS 决定
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def get_model(self, namespace: str) -> str | None:
        """读该 namespace 已登记的模型；未登记返回 None。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT model FROM embedding_index_meta WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return row["model"] if row else None

    def ensure_model(self, namespace: str, model: str) -> str:
        """校验或初始化该 namespace 的索引模型。

        首次写入时登记，之后每次比对。

        Returns:
            该 namespace 实际使用的模型（首次即传入值）。

        Raises:
            EmbeddingModelMismatchError: 已有索引由别的模型生成。
        """
        if not model or not model.strip():
            raise ValueError("model 不能为空 —— 空模型名会让校验失去意义")

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT model FROM embedding_index_meta WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO embedding_index_meta (namespace, model, created_at)"
                    " VALUES (?, ?, ?)",
                    (namespace, model, self._now_iso()),
                )
                return model
            if row["model"] != model:
                raise EmbeddingModelMismatchError(namespace, row["model"], model)
            return row["model"]


def ensure_index_model(
    settings: Settings,
    namespace: str,
    *,
    store: EmbeddingIndexMeta | None = None,
) -> str:
    """便捷入口：从 settings 取库路径与模型名，做一次校验。"""
    meta = store or EmbeddingIndexMeta(db_path_from_settings(settings))
    return meta.ensure_model(namespace, settings.EMBEDDING_MODEL)
