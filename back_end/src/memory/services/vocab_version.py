"""A23：词表版本化 —— 「今天打的标签，半年后还知道是按哪版词表打的」。

**为什么需要**：词表是代码里的常量。一旦改过（加一个类型、改一个关键词），
历史卡的 `types` 就失去了可解释性 —— 而 ADR 的整个标签体系建立在「可回溯」上。

设计取舍见 `specs/011-vocab-version`。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.labeler import DEFAULT_TYPE_RULES, FALLBACK_TYPE
from memory.services.raw_quote import db_path_from_settings

#: 历史卡没有版本号时的占位值。不是某个具体版本 —— 是「版本化之前」。
LEGACY_VERSION = "v0-legacy"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_registry (
    version    TEXT PRIMARY KEY,
    vocab_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def effective_vocab(settings: Settings) -> list[str]:
    """当前**实际生效**的类型词表，排序去重。

    与 `label_by_rules` / LLM 层的口径保持一致：

    - `TYPES_VOCAB` 非空 → 只认它（配置覆盖内置）
    - 为空 → 内置 `DEFAULT_TYPE_RULES` 的键 + `general` 兜底
    """
    if settings.TYPES_VOCAB:
        return sorted(set(settings.TYPES_VOCAB))
    return sorted(set(DEFAULT_TYPE_RULES) | {FALLBACK_TYPE})


def _fingerprint(vocab: list[str]) -> str:
    """词表内容的指纹。排序过，所以词表顺序变化不算版本变化。"""
    return hashlib.sha256("|".join(vocab).encode("utf-8")).hexdigest()[:8]


class VocabRegistry:
    """词表版本登记表。

    为什么要在 SQLite 里存一份词表原文:
    只存 hash 的话，「回溯某个版本」时只能告诉你「有这版」，
    却说不出「这版长什么样」—— 那和没有差不多。
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
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def current(self, settings: Settings) -> str:
        """拿到当前词表的版本号；词表变了就登记一个新版本。"""
        vocab = effective_vocab(settings)
        fp = _fingerprint(vocab)

        with self._lock, self._connect() as conn:
            # 先看这个指纹有没有登记过（同内容同版本，跨进程稳定）
            rows = conn.execute(
                "SELECT version, vocab_json FROM vocab_registry"
            ).fetchall()
            for row in rows:
                if row["version"].endswith(f"-{fp}"):
                    return row["version"]

            # 新版本：序号 = 已有版本数 + 1（从 v1 开始）
            n = len(rows) + 1
            version = f"v{n}-{fp}"
            conn.execute(
                "INSERT INTO vocab_registry (version, vocab_json, created_at)"
                " VALUES (?, ?, ?)",
                (version, json.dumps(vocab, ensure_ascii=False), self._now_iso()),
            )
            return version

    def get(self, version: str) -> list[str] | None:
        """按版本号取回词表原文；不存在返回 None（调用方应 404）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT vocab_json FROM vocab_registry WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["vocab_json"])

    def list_versions(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT version FROM vocab_registry ORDER BY created_at"
            ).fetchall()
        return [r["version"] for r in rows]


def current_version(settings: Settings, *, registry: VocabRegistry | None = None) -> str:
    """便捷入口：取当前版本号（首次调用会登记）。"""
    reg = registry or VocabRegistry(db_path_from_settings(settings))
    return reg.current(settings)
