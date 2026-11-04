"""A16：长文本切块（纯函数，无 IO）。

ADR D1 的设计是「一条事件卡切成多个 chunk，各存一个向量，检索命中的是 chunk，
返回前聚合成卡」。本模块只负责**切**，不碰存储和检索。

切分策略：**长度滑窗**。不做语义切分 —— 那需要模型，成本与收益都不划算。
相邻块重叠 `overlap` 个字符，避免「关键信息正好被切断」时两边都拿不到。

为什么按**字符**而不是 token：
- 中文按 token 切需要 tokenizer，而 chunk 边界不需要精确对齐 token
- 字符数与 token 数在中文里大致 1:1，在英文里 4:1 —— 用字符更保守（块更小）
- 纯函数依赖最小，可单测，无模型

上界保护：`size` 默认 256 字，远小于 bge-m3 的 8192 token 上限。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """一个文本块。`start`/`end` 是**原文本**里的字符下标，便于回溯。"""

    index: int
    text: str
    start: int
    end: int


def split_text(text: str, *, size: int = 256, overlap: int = 32) -> list[Chunk]:
    """把文本切成重叠的滑窗块。

    Args:
        text: 原文。空白会被 `strip()` 后再判断，但块内保留原样。
        size: 每块最大字符数，必须 > 0。
        overlap: 相邻块重叠字符数，必须 >= 0 且 < size。

    Returns:
        `list[Chunk]`。空文本或全空白返回 `[]`（不是 `[""]` —— 空块没有检索意义）。

    Raises:
        ValueError: `size <= 0` 或 `overlap < 0` 或 `overlap >= size`。
    """
    if size <= 0:
        raise ValueError(f"size 必须为正：{size}")
    if overlap < 0:
        raise ValueError(f"overlap 不能为负：{overlap}")
    if overlap >= size:
        raise ValueError(f"overlap({overlap}) 必须小于 size({size})，否则死循环")

    raw = text or ""
    if not raw.strip():
        return []

    # 短文本直接一块，不走滑窗 —— 避免 end 越界的边界判断
    if len(raw) <= size:
        return [Chunk(index=0, text=raw, start=0, end=len(raw))]

    chunks: list[Chunk] = []
    step = size - overlap
    start = 0
    index = 0
    while start < len(raw):
        end = min(start + size, len(raw))
        piece = raw[start:end]
        # 尾部只剩纯空白时不生成块（否则会多出一个没信息量的尾块）
        if piece.strip():
            chunks.append(Chunk(index=index, text=piece, start=start, end=end))
            index += 1
        if end >= len(raw):
            break
        start += step

    return chunks


def keep_top_n(
    scored: list[tuple[str, float]], *, n: int
) -> list[tuple[str, float]]:
    """每个父事件只保留分数最高的 `n` 个 chunk（A16 的去重规则）。

    Args:
        scored: `[(parent_event_id, score)]`，**一个 chunk 一项**，可含重复父 id。
        n: 每个父事件保留几个。`n <= 0` 时返回空列表（等价于「不保留」，
            调用方应先判断开关，不要用 0 表示「不限」）。

    Returns:
        保留的项，按分数降序（同分按出现顺序，保证确定性）。

    Note:
        这条规则是 ADR A16 的字面实现：「同 event_id 下 chunk 数 >
        CHUNK_KEEP_PER_EVENT 时，仅保留 rerank 最高分的 N 个」。
        它**不负责聚合** —— 聚合（chunk → 卡）是调用方的事。
    """
    if n <= 0:
        return []

    # 先按分数稳定降序，再按父 id 计数截断
    ordered = sorted(scored, key=lambda item: (-item[1], ))
    counts: dict[str, int] = {}
    kept: list[tuple[str, float]] = []
    for parent, score in ordered:
        used = counts.get(parent, 0)
        if used >= n:
            continue
        counts[parent] = used + 1
        kept.append((parent, score))
    return kept
