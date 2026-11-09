"""关系谓词（spec 039）：陈述通道 + 行为通道。

为什么要两条通道：

- **陈述**（「王泳负责前端」）精确但稀疏 —— 现有 566 条原文里
  `负责/维护/对接/汇报` 命中 **0**；
- **行为**（部署前端 3 次）稠密但要推断 —— 语料里部署类行为有 11 条。

只做陈述：一条边都抽不出来；只做行为：回答不了「谁**正式**负责」。
两条合并才有可用的关系层。

## 合并不变量

1. `exact`（陈述）**永远不被** `probable`（行为）降级
2. 行为可以给已有边**加证据**，但不能改写 label
3. 头尾类型不符直接拒（`OWNS: Person → Entity`）
4. 冲突（陈述指 A、行为指 B）→ 两条边都留，标 `conflict`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

#: 谓词表 v2。**删掉了 `REPORTS_TO`** —— `汇报/上级/领导/主管` 在语料里 0 命中，
#: 按「没有证据就先不定义」的规矩移除，将来有证据再加。
PREDICATE_SPECS: dict[str, tuple[str, str]] = {
    "OWNS": ("Person", "Entity"),
    "MAINTAINS": ("Person", "Entity"),
    "COLLABORATES_WITH": ("Person", "Person"),
    "DEPENDS_ON": ("Entity", "Entity"),
}

#: 行为动词 → 谓词。只收**职责性**动作：
#: `部署/发布/更新/重启/回滚` 是维护动作，`修改/修复/实现/新增` 是建设动作。
#: 刻意不收 `讨论/确认/汇报` —— 那些说明「参与过」，不说明「负责」。
VERB_TO_PREDICATE: dict[str, str] = {
    "部署": "MAINTAINS",
    "发布": "MAINTAINS",
    "更新": "MAINTAINS",
    "重启": "MAINTAINS",
    "回滚": "MAINTAINS",
    "维护": "MAINTAINS",
    "对接": "MAINTAINS",
    "修改": "OWNS",
    "修复": "OWNS",
    "实现": "OWNS",
    "新增": "OWNS",
}

#: 行为证据的门槛：同一 (人, 谓词, 目标) 至少几次独立事件才算「职责」。
#: 1 次可能只是临时帮忙；这条阈值必须可配（A3 要求 1 次不建边）。
DEFAULT_THRESHOLD = 2

#: 行为证据的有效期（天）。陈述没有有效期 —— 用户说的是事实，不会过期。
DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class RelationClaim:
    """一条候选关系。"""

    head: str
    predicate: str
    tail: str
    label: str = "probable"            # exact / probable / conflict
    confidence: float = 0.6
    evidence: tuple[str, ...] = ()     # event_id 列表（可回溯）
    source: str = "behavior"           # behavior / statement

    def key(self) -> tuple[str, str, str]:
        return (self.head, self.predicate, self.tail)


def predicate_for_verb(verb: str) -> str | None:
    """行为动词 → 谓词；不在表里的返回 None（不硬凑）。"""
    return VERB_TO_PREDICATE.get((verb or "").strip())


def aggregate_behaviors(
    rows: list[dict],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
) -> list[RelationClaim]:
    """把「人-动作-对象」行为行聚合成关系边。

    Args:
        rows: 每行 `{"person", "verb", "target", "event_id", "date"}`。
        threshold: 最少几次独立事件（A3：1 次不建边）。
        window_days: 只看最近 N 天（0/None 表示不限）。
        today: 基准日（测试用）。

    Returns:
        按证据数降序的 claim 列表。**同一个 event 只算一次证据**
        （卡片重放不会把数字刷大）。
    """
    if not rows:
        return []
    cutoff = None
    if window_days and window_days > 0:
        base = today or date.today()
        cutoff = base - timedelta(days=window_days)

    groups: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        person = (row.get("person") or "").strip()
        target = (row.get("target") or "").strip()
        verb = (row.get("verb") or "").strip()
        event_id = (row.get("event_id") or "").strip()
        if not person or not target or not event_id:
            continue
        predicate = predicate_for_verb(verb)
        if not predicate:
            continue
        day = _as_date(row.get("date"))
        if cutoff is not None and day is not None and day < cutoff:
            continue
        if predicate == "MAINTAINS" and person == target:
            continue  # 自己维护自己，没意义
        groups.setdefault((person, predicate, target), set()).add(event_id)

    claims: list[RelationClaim] = []
    for (head, predicate, tail), event_ids in groups.items():
        if len(event_ids) < threshold:
            continue
        claims.append(
            RelationClaim(
                head=head,
                predicate=predicate,
                tail=tail,
                label="probable",
                confidence=round(min(0.95, 0.5 + 0.1 * len(event_ids)), 2),
                evidence=tuple(sorted(event_ids)),
                source="behavior",
            )
        )
    claims.sort(key=lambda c: (-len(c.evidence), c.head, c.tail))
    return claims


#: 陈述句式（规则版）。刻意**不先用 LLM**：陈述语料本来就稀疏，
#: 规则可测、零成本；等真有大量陈述（分工文档/会议纪要）再上 LLM。
_STATEMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # (谓词, 正则模板) —— 用命名分组 head / tail
    ("OWNS", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*(?:负责|牵头|主管)\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,10})"),
    ("MAINTAINS", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*(?:维护|对接)\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,10})"),
    ("COLLABORATES_WITH", r"(?P<head>[\u4e00-\u9fa5]{2,4})\s*(?:和|与|跟)\s*(?P<tail>[\u4e00-\u9fa5]{2,4})\s*(?:一起|共同)"),
    ("DEPENDS_ON", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,10})\s*(?:依赖|依赖了|调用)\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,10})"),
)


#: 陈述句里「谁在说」的前缀词：`任泽安说王泳负责前端` 的主体是王泳，不是「任泽安说王泳」。
_SPEAKER_MARKERS = ("说", "表示", "提到", "称", "指出", "让", "请", "通知")

#: 尾值的截断符：`前端页面的维护` → `前端页面`
_TAIL_STOPS = ("的", "了", "和", "与", "并", "，", "。", "；", "、", "！", "？")


def _clean_head(raw: str) -> str:
    """去掉「谁在说」的前缀，只留被陈述的主体。"""
    text = (raw or "").strip()
    for marker in _SPEAKER_MARKERS:
        idx = text.rfind(marker)
        if idx > 0 and len(text) - idx - len(marker) >= 2:
            text = text[idx + len(marker):].strip()
    return text


def _clean_tail(raw: str) -> str:
    """在第一个截断符处断开，避免把「的维护」吃进目标名。"""
    text = (raw or "").strip()
    cut = len(text)
    for stop in _TAIL_STOPS:
        idx = text.find(stop)
        if 0 < idx < cut:
            cut = idx
    return text[:cut].strip()


def extract_statements(text: str) -> list[RelationClaim]:
    """从文本抽关系陈述（规则版，`label=exact`）。

    **只抽明确句式**：`X 负责 Y` / `X 维护 Y` / `X 和 Y 一起` / `X 依赖 Y`。
    宁可漏 —— 把叙述句误判成职责声明，比少一条边难修得多。
    """
    import re

    out: list[RelationClaim] = []
    for predicate, template in _STATEMENT_PATTERNS:
        pattern = re.compile(template)
        for match in pattern.finditer(text or ""):
            head = _clean_head(match.group("head"))
            tail = _clean_tail(match.group("tail"))
            if not head or not tail or head == tail:
                continue
            if len(head) < 2 or len(tail) < 2:
                continue
            out.append(
                RelationClaim(
                    head=head,
                    predicate=predicate,
                    tail=tail,
                    label="exact",
                    confidence=1.0,
                    evidence=(),
                    source="statement",
                )
            )
    return out


def merge_claims(claims: list[RelationClaim]) -> list[RelationClaim]:
    """合并同一 (head, predicate, tail) 的多条证据。

    - `exact` 优先：有任何陈述，label 就是 exact（**不被行为降级**）
    - 证据取并集（按 event_id 去重，保持稳定顺序）
    - 两条不同 tail 的冲突**不合并**：由调用方按 `conflict` 上报
    """
    merged: dict[tuple[str, str, str], RelationClaim] = {}
    for claim in claims:
        key = claim.key()
        prev = merged.get(key)
        if prev is None:
            merged[key] = claim
            continue
        evidence = tuple(sorted(set(prev.evidence) | set(claim.evidence)))
        label = "exact" if "exact" in (prev.label, claim.label) else claim.label
        confidence = max(prev.confidence, claim.confidence)
        source = "statement" if "statement" in (prev.source, claim.source) else claim.source
        merged[key] = RelationClaim(
            head=claim.head,
            predicate=claim.predicate,
            tail=claim.tail,
            label=label,
            confidence=confidence,
            evidence=evidence,
            source=source,
        )
    return list(merged.values())


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
