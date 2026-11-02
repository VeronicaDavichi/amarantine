"""B4/B5 LLM 打标器与双跑对比。

## B4：LLM 打标

规则层（`labeler.label_by_rules`）只能靠关键词命中，遇到「换工作」这种
没有词表词的句子会落到 `general`。LLM 层补的就是这个。

## B5：为什么要双跑而不是直接信 LLM

`types` 参与 `event_id` 的哈希，**打标一抖 = 同一件事写成两张卡**。
所以这里不信任何一层，而是：

1. 规则层先跑（永远有结果，是兜底）
2. LLM 层跑，只允许从词表里挑（`vocab` 约束，防自由生成）
3. 两边算 Jaccard；低于 `LABEL_AGREEMENT_MIN` → 记进 `LABEL_REVIEW` 队列

`LABEL_REVIEW` 和遗忘的 `NEED_REVIEW` 是**两条独立队列**（A24）：
前者是"这批卡打得对不对"，后者是"这张卡该不该留"。

## 为什么 LLM 失败就静默回退

写入路径不能因为 LLM 抖动而失败。规则层的结果一定合法（types 非空），
回退到它只是"打得粗一点"，不是错。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from memory.config import Settings
from memory.models.event_card import Action, Entity
from memory.services.labeler import DEFAULT_TYPE_RULES, FALLBACK_TYPE, LabelResult
from memory.services.llm import LLMError, chat

logger = logging.getLogger(__name__)

#: `Entity.kind` 是受控枚举，LLM 给的非法值一律退回 topic
_ALLOWED_KINDS = ("person", "topic", "place", "org", "event")

#: 两边一致度低于它 → 进 LABEL_REVIEW
DEFAULT_AGREEMENT_MIN = 0.5

#: LLM 输出的 JSON 可能被包在 ``` 里
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_SYSTEM = """你是事件打标器。给定一句话，选出它属于哪些类别。

规则：
- 只能从给定词表里选，不许自创
- 选 1–3 个最贴切的；实在不贴切就返回 ["general"]
- 同时抽取文中出现的具体实体（项目名、公司、人名、地点等）
- 同时抽取句中的**动作**：谁对谁做了什么
  - verb 用动词原形，不要带时态助词（`确定了` → `确定`）
  - patient 是这个动作的对象，可以是人或物，没有就填 null
  - 一句话有多个动作时全部列出，最外层那个 primary=true，其余 false
- 只输出 JSON，不要解释

输出格式：
{"types": ["decision", "work"],
 "entities": [{"name": "项目A", "kind": "topic"}],
 "actions": [{"verb": "决定", "patient": "项目A", "primary": true},
             {"verb": "延期", "patient": "项目A", "primary": false}]}"""

#: 一个事件最多保留几个动作（与规则层一致，防止长文本把图打散）
MAX_LLM_ACTIONS = 5


@dataclass
class LabelComparison:
    """规则层与 LLM 层的对比结果。"""

    rules: LabelResult
    llm: LabelResult | None
    agreement: float
    #: 一致度低于阈值时，需要人工看
    needs_review: bool
    #: 两边各自独有的标签，便于人快速判断
    only_rules: list[str] = field(default_factory=list)
    only_llm: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_types": list(self.rules.types),
            "llm_types": list(self.llm.types) if self.llm else None,
            "agreement": round(self.agreement, 3),
            "needs_review": self.needs_review,
            "only_rules": list(self.only_rules),
            "only_llm": list(self.only_llm),
        }


def jaccard(a: list[str], b: list[str]) -> float:
    """两个标签集合的 Jaccard 相似度。

    两边都空 → 1.0（"都没打出来"算一致，不该送复核）；
    一边空一边非空 → 0.0。
    """
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def compare_labels(
    rules: LabelResult,
    llm: LabelResult | None,
    *,
    agreement_min: float = DEFAULT_AGREEMENT_MIN,
) -> LabelComparison:
    """对比两层结果，判断要不要送人工复核。"""
    if llm is None:
        # LLM 没跑或失败 —— 不算"不一致"，不该拿去骚扰人
        return LabelComparison(
            rules=rules, llm=None, agreement=1.0, needs_review=False
        )
    score = jaccard(rules.types, llm.types)
    only_rules = sorted(set(rules.types) - set(llm.types))
    only_llm = sorted(set(llm.types) - set(rules.types))
    return LabelComparison(
        rules=rules,
        llm=llm,
        agreement=score,
        needs_review=score < agreement_min,
        only_rules=only_rules,
        only_llm=only_llm,
    )


# ---------------------------------------------------------------- LLM 调用


def _allowed_types(vocab: list[str] | None) -> list[str]:
    if vocab:
        return list(vocab)
    return [*DEFAULT_TYPE_RULES, FALLBACK_TYPE]


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出里抠出 JSON。容忍 ```json 围栏和前后废话。"""
    if not text:
        return None
    m = _FENCE.search(text)
    candidate = m.group(1) if m else text
    candidate = candidate.strip()
    # 找第一个 { 和最后一个 }
    lo, hi = candidate.find("{"), candidate.rfind("}")
    if lo == -1 or hi == -1 or hi <= lo:
        return None
    try:
        data = json.loads(candidate[lo : hi + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def make_llm_labeler(
    settings: Settings,
    *,
    vocab: list[str] | None = None,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> Callable[[str, list[str]], LabelResult | None]:
    """造一个 `label_event` 能用的 `llm_labeler` 回调。

    返回的函数签名是 ``(summary, rule_types) -> LabelResult | None``；
    **返回 None 表示"LLM 这层没结果"，调用方回退规则层**。
    任何异常都被吞掉并记日志 —— 写入路径不能因为 LLM 抖动而失败。
    """
    allowed = _allowed_types(vocab)
    allowed_str = "、".join(allowed)

    def labeler(summary: str, rule_types: list[str]) -> LabelResult | None:
        prompt = (
            f"可选类别：{allowed_str}\n\n"
            f"规则层初判：{rule_types or ['general']}\n\n"
            f"待打标的句子：{summary}"
        )
        try:
            raw = chat(
                [{"role": "user", "content": prompt}],
                settings,
                system=_SYSTEM,
                max_tokens=512,
                client=client,
                timeout_s=timeout_s,
            )
        except LLMError as exc:
            logger.warning("LLM 打标失败，回退规则层: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 兜住一切，写入优先
            logger.warning("LLM 打标异常，回退规则层: %s", exc)
            return None

        data = _parse_llm_json(raw)
        if data is None:
            logger.warning("LLM 打标输出不是合法 JSON: %r", raw[:200])
            return None

        raw_types = data.get("types")
        if not isinstance(raw_types, list):
            return None
        # 词表约束：只留认识的，防自由生成污染 event_id
        types = [t for t in raw_types if isinstance(t, str) and t in allowed]
        if not types:
            return None

        entities: list[Entity] = []
        raw_ents = data.get("entities")
        if isinstance(raw_ents, list):
            for item in raw_ents:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                # kind 是受控枚举，非法值退回 topic 而不是丢掉整条实体
                kind = str(item.get("kind") or "topic").strip()
                if kind not in _ALLOWED_KINDS:
                    kind = "topic"
                try:
                    entities.append(Entity(name=name, kind=kind))  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001 - 单个实体不合法就跳过
                    continue

        # 动作：LLM 补规则层漏掉的动词（规则层只有一张有限的动词表）
        actions: list[Action] = []
        raw_acts = data.get("actions")
        if isinstance(raw_acts, list):
            seen_verbs: set[str] = set()
            for item in raw_acts:
                if not isinstance(item, dict):
                    continue
                verb = str(item.get("verb") or "").strip()
                if not verb or verb in seen_verbs:
                    continue
                patient = item.get("patient")
                patient_str = str(patient).strip() if patient else None
                if patient_str in ("null", "None", ""):
                    patient_str = None
                seen_verbs.add(verb)
                try:
                    actions.append(
                        Action(
                            verb=verb,
                            patient=patient_str,
                            primary=bool(item.get("primary", not actions)),
                        )
                    )
                except Exception:  # noqa: BLE001 - 单个动作不合法就跳过
                    continue
                if len(actions) >= MAX_LLM_ACTIONS:
                    break
            # 保证有且只有一个 primary
            if actions and not any(a.primary for a in actions):
                actions[0] = Action(
                    verb=actions[0].verb,
                    patient=actions[0].patient,
                    primary=True,
                )

        return LabelResult(
            types=types, entities=entities, actions=list(actions), source="llm"
        )

    return labeler
