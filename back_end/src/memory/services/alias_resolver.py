"""别名的 LLM 推断：把描述解析成具体的名字。

规则层能处理的只有「显式等价」：

    糖包就是豆包          → exact，直接建边
    小张也叫张三          → exact，直接建边

碰到描述就没辙了：

    糖包就是那个AI里最弱智的   → uncertain，**不建边**

因为它右边不是名字。要落到「豆包」上，得先知道「最弱智的AI」是谁 ——
这一步规则层做不了，只能靠 LLM 从**已有记忆**里找。

## 为什么不让 LLM 自由生成

自由生成的名字会变成新节点，把图越撑越大，而且一旦错就是永久污染。
所以这里只做**选择题**：给 LLM 一份候选名单（图上已有的 Person / Entity），
它只能从里面挑，挑不出来就返回 null。

返回的名字还要再过一遍白名单校验 —— 防它编一个候选外的名字回来。

## 置信度不往上抬

LLM 说 `糖包 -> 豆包`，哪怕它写得斩钉截铁，也**永远不给 `exact`**。
`exact` 是「用户自己说的」，这是唯一的来源。LLM 推断最好只能到 `probable`。

这是 A29（有效引用回路）的同一个道理：用户的直接表述和系统的推断
不是一回事，混在一起之后就没法追溯「这条关系到底谁说的」。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from memory.config import Settings
from memory.models.event_card import (
    ALIAS_PROBABLE,
    ALIAS_PROBABLE_MIN,
    ALIAS_UNCERTAIN,
    AliasClaim,
)
from memory.services.llm import LLMError, chat

logger = logging.getLogger(__name__)

#: 给 LLM 的候选名单上限。
#:
#: 全量塞进去会撑爆上下文，而且候选越多选错的概率越高。
#: 只给最近的这些 —— 别名声明通常指向最近聊到的东西。
MAX_CANDIDATES = 40

#: 一次最多解析几条 claim。防一条句子把整个请求拖死。
MAX_CLAIMS_PER_CALL = 10

_SYSTEM = """你在做别名消歧。

用户记忆里有这样一句声明：「A 就是 B」，但 B 不是名字，而是一段描述
（比如「那个AI里最弱智的」「我们组那个新人」）。

你要判断 B 指的是**已知对象列表里的哪一个**。

规则：
- 只能从给出的已知对象里选，不许自己编名字
- 选不出来、或者不确定，canonical 返回 null
- confidence 是你的真实把握程度，不要凑数
- reason 用一句话说明依据

只返回 JSON，不要任何解释文字：
{"canonical": "选中的名字" 或 null, "confidence": 0.0-1.0, "reason": "依据"}"""


def _build_prompt(claim: AliasClaim, candidates: list[str]) -> str:
    cand_str = "、".join(candidates) if candidates else "（暂无已知对象）"
    return (
        f"已知对象：{cand_str}\n\n"
        f"待解析的声明：\n"
        f"  左：{claim.alias}\n"
        f"  右（描述）：{claim.canonical}\n\n"
        f"请判断「{claim.canonical}」指的是哪个已知对象。"
    )


def _parse_choice(raw: str) -> dict[str, Any] | None:
    """从 LLM 输出里抠出 JSON。模型爱加 ```json 围栏和前后废话。"""
    if not raw:
        return None
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _label_for(confidence: float) -> str | None:
    """置信度 → 标签。低于 `ALIAS_PROBABLE_MIN` 就丢掉。"""
    if confidence >= ALIAS_PROBABLE_MIN:
        return ALIAS_PROBABLE
    if confidence >= 0.5:
        return ALIAS_UNCERTAIN
    return None


def resolve_one(
    claim: AliasClaim,
    candidates: list[str],
    *,
    settings: Settings,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> AliasClaim | None:
    """解析一条描述性 claim。解析不出来返回 None。

    **只接受 `uncertain` 的输入**：`exact` 是用户自己说的，不该再问 LLM；
    `probable` 已经推过一轮了，重复解析只会来回改。
    """
    if claim.label != ALIAS_UNCERTAIN:
        return None
    if not candidates:
        return None

    allowed = {c for c in candidates if c and c != claim.alias}
    if not allowed:
        return None

    prompt = _build_prompt(claim, sorted(allowed)[:MAX_CANDIDATES])
    try:
        raw = chat(
            [{"role": "user", "content": prompt}],
            settings,
            system=_SYSTEM,
            # 1024 不是随便定的：deepseek-v4-flash 默认先输出 thinking 块，
            # 配额太小的话正文会被截断成 `{"canonical": "豆包", "confidence":`
            # 甚至一个字都不剩 —— 实测踩过，512 都不够，JSON 直接解析失败。
            max_tokens=1024,
            client=client,
            timeout_s=timeout_s,
        )
    except LLMError as exc:
        logger.warning("LLM 别名解析失败 | %s -> %s | %s", claim.alias, claim.canonical, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 推断是增强，不能拖垮调用方
        logger.warning("LLM 别名解析异常 | %s", exc)
        return None

    data = _parse_choice(raw)
    if data is None:
        logger.warning("LLM 别名输出不是合法 JSON: %r", raw[:200])
        return None

    picked = str(data.get("canonical") or "").strip()
    if not picked:
        logger.info(
            "LLM 判定无法解析 | %s -> %s | %s",
            claim.alias,
            claim.canonical,
            data.get("reason"),
        )
        return None

    # 白名单校验：模型可能编一个候选外的名字回来。
    # 不做这一步的话，一句描述就能在图里凭空造出一个新节点。
    if picked not in allowed:
        logger.warning(
            "LLM 返回了候选外的名字，丢弃 | %r 不在 %d 个候选里",
            picked,
            len(allowed),
        )
        return None

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    label = _label_for(confidence)
    if label is None:
        logger.info(
            "LLM 置信度过低，丢弃 | %s -> %s conf=%.2f", claim.alias, picked, confidence
        )
        return None

    logger.info(
        "LLM 解析别名 | %s -> %s (%s %.2f) | %s",
        claim.alias,
        picked,
        label,
        confidence,
        data.get("reason"),
    )
    return AliasClaim(
        alias=claim.alias,
        canonical=picked,
        label=label,  # type: ignore[arg-type]
        # **永远不是 user_declared**：这条是系统推的，追溯时要能分开。
        source="llm_inferred",
    )


def resolve_claims(
    claims: list[AliasClaim],
    *,
    candidates: list[str],
    settings: Settings,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> list[AliasClaim]:
    """批量解析，返回**新推断出来的** claim（原 claim 不会出现在结果里）。

    只处理 `uncertain` 的。别的一律跳过 —— 见 `resolve_one` 的说明。
    """
    todo = [c for c in claims if c.label == ALIAS_UNCERTAIN][:MAX_CLAIMS_PER_CALL]
    if not todo:
        return []

    resolved: list[AliasClaim] = []
    for claim in todo:
        got = resolve_one(
            claim,
            candidates,
            settings=settings,
            client=client,
            timeout_s=timeout_s,
        )
        if got is not None:
            resolved.append(got)
    return resolved
