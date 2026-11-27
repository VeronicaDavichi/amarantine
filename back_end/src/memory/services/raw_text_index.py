# -*- coding: utf-8 -*-
"""原文分词索引（spec 051）：让「我记得有句话…」能找回那张卡。

## 为什么需要

召回链路里向量与 BM25 **都只覆盖摘要**，而每张卡是 30 条原文窗口、
摘要只写其中一个主要话题（实测 90% 的样本原话与摘要零内容交集）。
所以「我记得有句话」这类查询根本没有可命中的索引。

诊断数据见 [spec 051](../../../specs/051-raw-quote-index/README.md)：
只索引摘要时 hit@5 = 0.42；模拟「原文也建索引」上界 0.917。

## 这里放什么

- `should_index()`：**两道闸**（去营销、去 QQ 卡片 JSON）
- `build_tokens()`：拼窗口原文 → 分词 → 截断

闸只写这一处，索引、材料组装、reindex 全走它 ——
三处各写一份的话，「索引里有、材料里没有」这种错配极难查。
"""

from __future__ import annotations

import logging
import re

from memory.core.tokenize import tokenize_for_index
from memory.services.marketing import looks_like_marketing

logger = logging.getLogger("memory.raw_text_index")

#: QQ 小程序/公告卡片被导出成结构化 JSON 的特征。
#: 实测样例（去营销后最长的几条都是它）：
#: `{"app":"com.tencent.mannounce","bizsrc":"","config":{"ctime":…`
#: 这类东西进 BM25 只会灌 `app` / `config` / `bizsrc` 之类噪声词。
_STRUCTURED_RE = re.compile(r'\{\s*"(app|ver|config|bizsrc|prompt|meta)"\s*:')

#: 单条原文少于这个长度就不进索引：`[图片]`、`哈哈哈`、`草` 这类没有检索价值，
#: 却会大量命中短查询（污染 BM25）。
MIN_QUOTE_CHARS = 4

#: 占位符（`[图片]` / `[表情]` / `@某人`）不算内容 —— 它们占长度但没有语义。
#: 踩过：`[图片]` 正好 4 个字符，按裸长度算会混进索引。
_PLACEHOLDER_RE = re.compile(r"\[[^\]]{0,12}\]|@\S+")

#: 拼接分隔符。**必须是有分隔的** —— 直接相接会造出跨条边界的假 bigram
#: （上一条结尾 + 下一条开头 组成一个词），那是纯粹的噪声。
JOIN_SEP = "\n"

#: 索引里保留的最大字符数（截断前）
DEFAULT_MAX_CHARS = 4000


def should_index(text: str) -> bool:
    """这条原文该不该进索引 / 出现在材料里。

    Returns:
        True = 该进；False = 营销、结构化卡片、太短、空。
    """
    body = (text or "").strip()
    if not body:
        return False
    # 占位符先剥掉再量长度：`[图片]` 是 4 个字符，但一个信息量都没有
    visible = _PLACEHOLDER_RE.sub("", body).strip()
    if len(visible) < MIN_QUOTE_CHARS:
        return False
    if _STRUCTURED_RE.search(body):
        return False
    if looks_like_marketing(body)[0]:
        return False
    return True


def filter_quotes(texts: list[str]) -> list[str]:
    """过两道闸，保持原顺序。"""
    return [t for t in (texts or []) if should_index(t)]


def build_tokens(texts: list[str], *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """把窗口原文拼成 BM25 用的 token 串。

    Args:
        texts: 原文列表（顺序即时间顺序）。
        max_chars: 截断上限（**截断在分词前**，避免切出半个词）。

    Returns:
        空格分隔的 token 串；没有可用原文时返回空串。
    """
    kept = filter_quotes(texts)
    if not kept:
        return ""
    joined = JOIN_SEP.join(kept)
    if max_chars > 0 and len(joined) > max_chars:
        # 按行截断优先：宁可少一整条，也不要把一条消息砍成半句
        out: list[str] = []
        used = 0
        for line in kept:
            if used + len(line) > max_chars and out:
                break
            out.append(line)
            used += len(line) + len(JOIN_SEP)
        joined = JOIN_SEP.join(out) if out else joined[:max_chars]
    return tokenize_for_index(joined)


def material_quotes(
    texts: list[str],
    *,
    max_chars: int,
    budget_left: int,
) -> tuple[list[str], int, bool]:
    """给召回材料挑原文（spec 051 的贪心填充）。

    Args:
        texts: 该卡的原文列表（已按时间序）。
        max_chars: 单卡上限（`RAW_MATERIAL_MAX_CHARS`，默认 2000）。
        budget_left: 剩余预算（字符）。

    Returns:
        `(给出去的原文, 消耗字符数, 是否被截断)`。

        - `budget_left <= 0` → 返回 `([], 0, False)`：**没给**，不是「给了空的」
        - 卡本身没有可用原文 → `([], 0, False)`
        - 有原文但被上限/预算砍过 → 第三项为 True
    """
    kept = filter_quotes(texts)
    if not kept or budget_left <= 0:
        return [], 0, False
    cap = min(max_chars, budget_left)
    out: list[str] = []
    used = 0
    for line in kept:
        cost = len(line) + (1 if out else 0)
        if used + cost > cap:
            break
        out.append(line)
        used += cost
    truncated = len(out) < len(kept)
    return out, used, truncated


def enabled(settings: object) -> bool:
    """该不该建索引（开关默认关，与 `CHUNK_ENABLED` 同口径）。"""
    return bool(getattr(settings, "RAW_TEXT_INDEX_ENABLED", False))


def index_max_chars(settings: object) -> int:
    return int(getattr(settings, "RAW_TEXT_INDEX_MAX_CHARS", DEFAULT_MAX_CHARS))


def material_max_chars(settings: object) -> int:
    return int(getattr(settings, "RAW_MATERIAL_MAX_CHARS", 2000))


def index_cards(
    *,
    namespace: str,
    event_ids: list[str],
    quote_ids: list[str],
    settings: object,
    raw_store: object | None = None,
    store: object | None = None,
) -> int:
    """给这批卡写原文分词。返回真正写入的张数。

    **失败只告警不抛**：索引是增强项，不能让「导 200 条时 Weaviate 抖一下」
    把整批导库标成失败。

    Note:
        同一批的卡共享同一个 `raw_quote_ids`（spec 005 的写法：整批 quote 挂在
        每张卡上），所以它们的 token 串是**相同**的 —— 算一次、写 N 次。
    """
    if not enabled(settings) or not event_ids or not quote_ids:
        return 0
    try:
        if raw_store is None:
            from memory.services.raw_quote import RawQuoteStore, db_path_from_settings

            raw_store = RawQuoteStore(db_path_from_settings(settings))  # type: ignore[arg-type]
        texts = raw_store.texts_for(namespace, list(quote_ids))  # type: ignore[attr-defined]
        tokens = build_tokens(texts, max_chars=index_max_chars(settings))
        if not tokens:
            return 0
        if store is None:
            from memory.services.weaviate_store import WeaviateStore

            store = WeaviateStore(settings)  # type: ignore[arg-type]
        written = 0
        for eid in event_ids:
            store.set_raw_text_tokens(  # type: ignore[attr-defined]
                eid, namespace, tokens=tokens
            )
            written += 1
        logger.info(
            "原文索引写入 | ns=%s cards=%d tokens=%d",
            namespace, written, len(tokens.split()),
        )
        return written
    except Exception as exc:  # noqa: BLE001 - 增强项，绝不阻断主流程
        logger.warning("原文索引写入失败（已忽略）: %s", exc)
        return 0

#: 向量文本的字符上限。与索引上限分开：索引怕对象变大，向量怕服务端超时。
#: 实测 bge-m3 在 CPU 上 780 字约 2.6 秒；4000 字会接近 10 秒，太慢。
VECTOR_MAX_CHARS = 1200


def build_vector_text(
    texts: list[str],
    *,
    fallback: str,
    max_chars: int = VECTOR_MAX_CHARS,
) -> tuple[str, str]:
    """给**向量**用的文本（spec 052）：窗口内原文，而不是摘要。

    ## 为什么换源

    摘要 P50 只有 17 字，而一张卡覆盖 30 条原文（约 780 字）。
    向量只编码摘要 = 窗口里 97% 的信息在向量空间不存在。
    实测（279 张卡池内，60 样本）：hit@1 0.417→0.550，MRR 0.465→0.558。

    ## 两道闸 + 回退

    - 营销长文 / QQ 卡片 JSON 不进向量（噪声直接进语义空间最糟）
    - 全被闸挡掉时**退回 summary**（否则这张卡没法写进向量库）
      实测这种卡 23 张 / 4383（0.5%）

    Returns:
        `(文本, 来源)`；来源 ∈ `raw` / `summary`，便于调用方记日志与断言。
    """
    kept = filter_quotes(texts)
    if not kept:
        return (fallback or ""), "summary"
    doc = JOIN_SEP.join(kept)
    if max_chars > 0 and len(doc) > max_chars:
        # 按整条截断：宁可少一整条，也不要把一条消息砍成半句
        out: list[str] = []
        used = 0
        for line in kept:
            if used + len(line) > max_chars and out:
                break
            out.append(line)
            used += len(line) + len(JOIN_SEP)
        doc = JOIN_SEP.join(out) if out else doc[:max_chars]
    return doc, "raw"
