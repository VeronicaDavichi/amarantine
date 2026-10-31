"""B4 打标 ①② 层：规则层 + LLM 层。

两层分工（ADR D4）：

- **规则层**：关键词/正则命中，零成本、可复现、可解释 → 先跑
- **LLM 层**：结构化输出补长尾，候选集受 `TYPES_VOCAB` 约束 → 后跑

P0 决定：规则层**必须**独立可用（LLM 挂了也能写卡），LLM 层通过注入才启用。
这样 A21 的打标验收可以先用手工规则基线跑起来，再接 LLM 提升。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory.models.event_card import Entity

#: 规则层内置词表（`TYPES_VOCAB` 为空时才用）。类型值应保持稳定，便于回溯统计。
DEFAULT_TYPE_RULES: dict[str, tuple[str, ...]] = {
    "decision": ("决定", "定了", "拍板", "敲定", "确定为", "就这么办"),
    "work": ("项目", "需求", "上线", "版本", "排期", "会议", "汇报", "代码", "部署"),
    "study": ("学习", "考试", "课程", "论文", "读书", "练习"),
    "health": ("体检", "住院", "生病", "就医", "手术", "复查"),
    "relationship": ("朋友", "家人", "同事", "伴侣", "结婚", "分手", "吵架"),
    "emotion": ("开心", "难过", "生气", "焦虑", "兴奋", "沮丧", "委屈"),
    "travel": ("出差", "旅行", "航班", "酒店", "签证"),
    "money": ("借钱", "还款", "工资", "报销", "预算", "投资"),
}

#: 兜底类型：任何规则都没命中时使用（保证 types 非空，否则 EventCard 校验失败）
FALLBACK_TYPE = "general"

#: 实体抽取规则：常见中文命名实体后缀。
#: 只做高置信度抽取——错抽比漏抽更糟，会把无关事件图连起来。
_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("topic", re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?项目)")),
    ("topic", re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?系统)")),
    ("org", re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?公司)")),
    ("org", re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?团队)")),
    ("place", re.compile(r"([\u4e00-\u9fa5]{2,10}(?:市|区|路|街))")),
)

#: 过度泛化的实体名，抽到也不要
_STOP_ENTITIES = frozenset({"这个项目", "那个项目", "公司", "团队", "项目", "系统"})


@dataclass(frozen=True)
class LabelResult:
    """打标产物。`source` 便于统计规则命中率，也便于 A22 的双跑对比。"""

    types: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    source: str = "rules"


def _match_types(summary: str, vocab: list[str]) -> list[str]:
    """按词表匹配类型；命中多个时全保留（多类型在 ADR 里是允许的）。"""
    rules = {k: v for k, v in DEFAULT_TYPE_RULES.items() if not vocab or k in vocab}
    if vocab:
        rules = {k: DEFAULT_TYPE_RULES.get(k, ()) for k in vocab}
    hits = [
        type_name
        for type_name, keywords in rules.items()
        if any(keyword in summary for keyword in keywords)
    ]
    return hits or [FALLBACK_TYPE]


def _extract_entities(summary: str) -> list[Entity]:
    """按后缀规则抽实体，去重且保序。"""
    seen: set[str] = set()
    entities: list[Entity] = []
    for kind, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(summary):
            name = match.group(1).strip()
            if name in _STOP_ENTITIES or len(name) < 2:
                continue
            if name in seen:
                continue
            seen.add(name)
            entities.append(Entity(name=name, kind=kind))
    return entities


def extract_entities(text: str) -> list[Entity]:
    """从自由文本抽实体（召回时从 query 里抽，用于图通道）。

    与打标共用同一套规则，保证「写入时抽到的实体」和「查询时抽到的实体」
    口径一致——否则图通道会系统性漏检。
    """
    return _extract_entities(text)


def label_by_rules(summary: str, *, vocab: list[str] | None = None) -> LabelResult:
    """规则层打标：永远给出非空 types（否则事件卡无法落库）。"""
    types = _match_types(summary, vocab or [])
    entities = _extract_entities(summary)
    return LabelResult(types=types, entities=entities, source="rules")


def label_event(
    summary: str,
    *,
    vocab: list[str] | None = None,
    llm_labeler: object | None = None,
) -> LabelResult:
    """两层打标入口。

    Args:
        summary: 事件摘要。
        vocab: `TYPES_VOCAB`；为空表示用内置词表。
        llm_labeler: 可选的可调用对象，签名 ``(summary, candidates) -> LabelResult | None``。
            返回 None 时回退规则层结果（LLM 失败不该阻断写入）。

    Returns:
        LabelResult；types 保证非空。
    """
    base = label_by_rules(summary, vocab=vocab)
    if llm_labeler is None:
        return base

    try:
        refined = llm_labeler(summary, base.types)  # type: ignore[operator]
    except Exception:  # noqa: BLE001 - LLM 层失败必须降级，不影响写卡
        return base

    if not isinstance(refined, LabelResult) or not refined.types:
        return base
    # 候选集约束：LLM 只能从词表里挑，防止自由生成
    allowed = set(vocab) if vocab else set(DEFAULT_TYPE_RULES) | {FALLBACK_TYPE}
    filtered = [t for t in refined.types if t in allowed] or base.types
    return LabelResult(types=filtered, entities=refined.entities or base.entities, source="llm")
