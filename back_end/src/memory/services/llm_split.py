"""B2 LLM 事件切分。

## 问题

用户一次说的话常常包含多件事：

> 昨天跟张三开会定了项目A延期到六月，然后我去体检，医生说血压有点高

如果整段当成一个事件卡，`types` 会同时是 `decision`+`work`+`health`，
`event_id` 也把三件事混在一起 —— 之后想单独忘掉「体检」那部分就做不到
（遗忘的粒度就是事件卡）。

## 做法

让 LLM 切成独立事件，每条都能单独成卡。约束：

- **只拆不合**：不润色、不补全、不合并
- **原文可追溯**：每条都要能在原文里找到依据，不许凭空生成
- **失败不阻断**：LLM 挂了就返回 `[text]` 一条，写入照常

## 为什么不做「拆完再判重」

拆出来的两条如果语义重复，会各自生成不同的 `event_id`（哈希不同），
变成两张卡。这确实是 B2 的已知边界，但**判重放在切分阶段风险更大**：
把两条本来不同的事判成一条，是不可逆的信息丢失；写重复卡只是浪费空间，
而且能被遗忘策略回收。所以这里选择「宁可重复，不可丢失」。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from memory.config import Settings
from memory.services.llm import LLMError, chat
from memory.services.llm_labeler import _parse_llm_json

logger = logging.getLogger("memory.llm_split")

#: 单条事件的长度上界（字符）。超了说明 LLM 没真拆开，退回原文
MAX_EVENT_CHARS = 300

#: 最多拆几条。防 LLM 把一句话拆成 50 条垃圾
MAX_EVENTS = 12

_SYSTEM = """你是事件切分器。把用户说的一段话拆成若干条独立事件。

规则：
- 每一条必须是**能单独成立**的事件，不要互相依赖
- 只拆不合：不润色、不补全、不合并
- 每条都要能在原文里找到依据，不许编造
- 每条的表述尽量独立，不要用「然后」「另外」开头
- 如果整段只讲一件事，就返回一条
- 只输出 JSON 数组，不要解释

输出格式：
["张三决定项目A延期到六月", "体检发现血压偏高"]"""


def _looks_like_echo(parts: list[str], text: str) -> bool:
    """拆出来的东西明显不对（原文照抄一份 / 自创内容）就判无效。"""
    if not parts:
        return True
    if len(parts) > MAX_EVENTS:
        return True
    if len(parts) == 1 and parts[0].strip() == text.strip():
        # 单条且等于原文 —— 这就是"没拆"，不算失败，但也没什么可写的
        return False
    # 每条过长 → 大概率是 LLM 把整段塞进一条
    return any(len(p) > MAX_EVENT_CHARS for p in parts)


def split_events(
    text: str,
    settings: Settings,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> list[str]:
    """把一段话拆成多条事件摘要。

    Args:
        text: 用户原话。
        settings: 提供 LLM 配置。
        client: 可注入的 httpx.Client（测试用）。
        timeout_s: 超时秒数。

    Returns:
        事件摘要列表，**至少一条**（就是原文本身）。
        LLM 失败、输出不合法、拆分结果异常时都退回 `[text]`。

    Note:
        这是"尽力而为"的增强，不是写入路径的必需环节。任何问题都降级。
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    # 太短的句子不值得付一次 LLM 调用
    if len(stripped) <= 40 and "\n" not in stripped:
        return [stripped]

    prompt = f"待切分的文本：\n{stripped}"
    try:
        raw = chat(
            [{"role": "user", "content": prompt}],
            settings,
            system=_SYSTEM,
            max_tokens=1024,
            client=client,
            timeout_s=timeout_s,
        )
    except LLMError as exc:
        logger.warning("LLM 切分失败，整段当一条: %s", exc)
        return [stripped]
    except Exception as exc:  # noqa: BLE001 - 写入优先
        logger.warning("LLM 切分异常，整段当一条: %s", exc)
        return [stripped]

    parts = _parse_array(raw)
    if parts is None:
        logger.warning("LLM 切分输出不是合法数组: %r", raw[:200])
        return [stripped]

    parts = [p.strip() for p in parts if p and p.strip()]
    if _looks_like_echo(parts, stripped):
        logger.warning("LLM 切分结果异常（过多或过长），整段当一条")
        return [stripped]
    if not parts:
        return [stripped]
    return parts[:MAX_EVENTS]


def _parse_array(text: str) -> list[str] | None:
    """从输出里抠出 JSON 数组。容忍 ``` 围栏和前后废话。"""
    if not text:
        return None
    candidate = text.strip()
    lo, hi = candidate.find("["), candidate.rfind("]")
    if lo == -1 or hi == -1 or hi <= lo:
        # 也接受 {"events": [...]} 这种包了一层的
        data = _parse_llm_json(text)
        if data is None:
            return None
        for key in ("events", "items", "result"):
            val = data.get(key)
            if isinstance(val, list):
                return [str(x) for x in val if isinstance(x, (str, int, float))]
        return None
    try:
        data = json.loads(candidate[lo : hi + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out: list[str] = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # 容忍 [{"summary": "..."}]
            for key in ("summary", "event", "text"):
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
    return out
