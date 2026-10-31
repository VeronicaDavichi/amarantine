"""B7 `event_id` 生成 —— 幂等地基。

规则来自 ADR D1：

    canonical  = namespace + "|"
               + date_start + "|" + (date_end ?? "") + "|"
               + sorted(types).join(",") + "|"
               + sorted(entity_names).join(",") + "|"
               + normalize(summary)
    event_id   = "evt_" + sha256(canonical)[0:16]
    normalize  = 去首尾空白、全角转半角、转小写、去标点

**namespace 必须参与哈希**：否则两个 agent 记同一句话会撞 id，
幂等机制把 A 的写入误判成 B 的重复（ADR D1 明确要求）。

设计取舍：这里是纯函数，不碰 IO —— 幂等性靠「同样输入永远同样输出」保证，
而不是靠查库。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from typing import Iterable

#: 标点与符号（保留字母、数字、下划线、空白与 CJK 汉字）
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

#: event_id 前缀与摘要长度（16 个 hex = 64 bit，碰撞概率对本场景足够）
PREFIX = "evt_"
HASH_LEN = 16


def normalize_summary(summary: str) -> str:
    """规范化摘要，让「同一件事的细微写法差异」落到同一个 id。

    - NFKC：全角转半角（`，` → `,`、`Ａ` → `A`）
    - strip + lower：「  用户 说了 」与「用户说了」等价
    - 去标点：「记住了！」与「记住了」等价
    """
    text = unicodedata.normalize("NFKC", summary)
    text = text.strip().lower()
    text = _PUNCT.sub("", text)
    return text


def make_event_id(
    *,
    namespace: str,
    date_start: date,
    date_end: date | None,
    types: Iterable[str],
    entity_names: Iterable[str],
    summary: str,
) -> str:
    """按 ADR 规则生成内容哈希主键。

    Args:
        namespace: 记忆域，参与哈希（防跨域撞 id）。
        date_start: 事件起始日。
        date_end: 事件结束日；``None`` 表示持续中，参与哈希为空串。
        types: 类型标签集合（内部排序后拼接，与顺序无关）。
        entity_names: 实体名集合（同上）。
        summary: 事件摘要。

    Returns:
        形如 ``evt_0123456789abcdef`` 的 id。
    """
    canonical = build_canonical(
        namespace=namespace,
        date_start=date_start,
        date_end=date_end,
        types=types,
        entity_names=entity_names,
        summary=summary,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PREFIX + digest[:HASH_LEN]


#: canonical 拼装函数单独导出，便于测试与排障时复现
def build_canonical(
    *,
    namespace: str,
    date_start: date,
    date_end: date | None,
    types: Iterable[str],
    entity_names: Iterable[str],
    summary: str,
) -> str:
    """返回参与哈希的规范化字符串（排障用，不含摘要）。"""
    return "|".join(
        (
            namespace,
            date_start.isoformat(),
            date_end.isoformat() if date_end is not None else "",
            ",".join(sorted(types)),
            ",".join(sorted(entity_names)),
            normalize_summary(summary),
        )
    )
