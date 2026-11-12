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
from dataclasses import dataclass, field
from typing import Any

from memory.config import Settings
from memory.services.llm import LLMError, chat
from memory.services.llm_labeler import _parse_llm_json

logger = logging.getLogger("memory.llm_split")

#: 单条事件的长度上界（字符）。超了说明 LLM 没真拆开，退回原文
MAX_EVENT_CHARS = 300

#: 最多拆几条。防 LLM 把一句话拆成 50 条垃圾
#: 一次切分最多接受多少条事件。
#:
#: **这是「防过度切分」的上限，不是质量门槛**：超了就截断（`parts[:MAX_EVENTS]`），
#: 不判为异常。
#:
#: 实测（spec 034，DingTalk 32 轮日常对话）：LLM 切出 **46 条**，
#: 平均每轮 1.4 条 —— 完全合理。原值 12 会把这种正常结果整段丢弃。
#:
#: 为什么不能凭「条数多」判异常：一段 30 轮的工作对话本来就该切出十几到几十条。
#: 真正该判异常的是「一条特别长」（`MAX_EVENT_CHARS`）或「单条等于原文」（没拆）。
MAX_EVENTS = 40

#: 递归二分的最大轮次（spec 036）。
#:
#: 一段话切出超过 `MAX_EVENTS` 条时，二分后重新切，而不是截断。
#: 3 轮 = 最多把一段分成 8 份：实测最坏一次切出 65 条（上限的 1.6 倍），
#: 二分一次（2 × ~33 条）就够；留到 3 轮是给「单批 200+ 轮对话」的余量。
#: 还超就截断，但**必须**记 `dropped` 并打 WARNING —— 不许静默。
MAX_SPLIT_ROUNDS = 3


@dataclass(frozen=True)
class SplitResult:
    """`split_all()` 的结果。

    Attributes:
        parts: 最终摘要列表，**已截断**到保护上限内。
        rounds: 实际用掉的二分轮次；没二分过就是 1（= 「只切了一轮」）。
        dropped: 到轮次上限仍超限而丢掉的条数。0 表示一条没丢。

    Note:
        spec 036 原本写的是 `(parts, rounds)` 二元组；实现时发现
        「丢了几条」必须一路带到 job 里 —— 否则截断又变回**静默**。
        所以加成了三个字段。
    """

    parts: list[str]
    rounds: int
    dropped: int = 0

    def __bool__(self) -> bool:
        return bool(self.parts)

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
    """拆出来的东西**质量**不对就判无效（原文照抄 / 单条过长）。

    **不再判「条数过多」**（spec 034）：那个判断和 `split_events` 末尾的
    `parts[:MAX_EVENTS]` 直接冲突 —— 超过 `MAX_EVENTS` 本该**截断**，
    但因为这里先返回 True，截断那行永远执行不到，结果是整段丢弃。

    实测损失：32 轮对话被切成 46 条（合理），却因为 46 > 12 被判异常，
    最后产出 1 张 summary 是原文拼接的垃圾卡，而且**不报错**。
    """
    if not parts:
        return True
    if len(parts) == 1 and parts[0].strip() == text.strip():
        # 单条且等于原文 —— 这就是"没拆"，不算失败，但也没什么可写的
        return False
    # 每条过长 → 大概率是 LLM 把整段塞进一条
    return any(len(p) > MAX_EVENT_CHARS for p in parts)


def _split_once(
    text: str,
    settings: Settings,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> list[str]:
    """切一次，**不截断**：LLM 返回多少条就给多少条。

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

        截断**不在这里做**：两个调用方对「超限」的处理不同
        （`split_events` 截断、`split_all` 二分），在这里盖掉信息就没得选了。
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    # 太短的句子不值得付一次 LLM 调用
    if len(stripped) <= 40 and "\n" not in stripped:
        return [stripped]

    prompt = f"待切分的文本：\n{stripped}"
    try:
        # `max_tokens` 必须按输入长度给，不能像 `alias_resolver` 那样写死 1024。
        #
        # 实测（spec 034，DingTalk 语料 1583 字 / 32 轮）：
        # `deepseek-v4-flash` 默认**先输出 thinking 块**。1024 会被 thinking
        # 吃光，正文截断成空 -> `_parse_array` 失败 -> 整段当一条 ->
        # 产出的卡 `summary` 是原文拼接（不是事件摘要），**而且不报错**
        # （设计上就是降级，这是最难发现的地方）。
        #
        # **踩过的坑**：第一版公式 `2048 + len(text)` = 3631 仍然不够。
        # 那批对话切出了 **46 条**摘要（1565 字 ≈ 1000+ token），
        # 加上 thinking 就超了。字符数估 token 本来就偏小 ——
        # 中文 1 字 ≈ 0.6-1 token，thinking 是英文（1 token ≈ 4 字符），
        # 两段加起来很难用一个简单公式算准。
        #
        # **踩的第二个坑**：`2048 + len*2` = 5214 仍然不够。
        # 那次输出是**被截断的 JSON**（缺闭合 `]`），`_parse_array` 返回 None，
        # 报「不是合法数组」—— 和「返回空」看起来完全不同，但根因一样。
        #
        # 结论：按字符数估 token 这条路走不通（中文、英文 thinking、
        # 输出条数三个变量互相影响）。`max_tokens` 是**上限不是目标**，
        # 给大不会让输出变长，只会避免截断 —— 所以直接给足。
        _est = max(8192, min(16384, len(stripped) * 3))
        logger.info(
            "LLM 切分参数 | 输入 %d 字 | max_tokens=%d | timeout_s=%s",
            len(stripped), _est, timeout_s,
        )
        raw = chat(
            [{"role": "user", "content": prompt}],
            settings,
            system=_SYSTEM,
            max_tokens=_est,
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
    return parts


def split_events(
    text: str,
    settings: Settings,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> list[str]:
    """把一段话拆成多条事件摘要（**结果截断到 `MAX_EVENTS`**）。

    单条写入路径用的老接口：一次对话最多产 `MAX_EVENTS` 张卡，超了就丢尾部。
    批量导入请用 `split_all()` —— 它会二分，而不是丢。

    Returns:
        事件摘要列表，空输入返回 `[]`。
    """
    return _split_once(text, settings, client=client, timeout_s=timeout_s)[:MAX_EVENTS]




#: 带上下文切分的系统提示。三条约束都是硬要求：
#: 1. 上文只用于理解指代，**不能为它建卡**（否则回溯内容会变成新事件）
#: 2. 每条事件必须带本片内的源消息序号（建卡范围因此被锁在原始 30 条内）
#: 3. 看不懂就直说 `needs_more_context`，系统会拉更长上文重试一次
_SYSTEM_SOURCED = """你是事件切分器。我会给你两段文本：
- 【上文】：之前的对话，**只用来理解指代**（「那个」「他」指谁），不要为它建卡
- 【待切分】：本次要处理的 {n} 条消息

把【待切分】拆成若干条独立事件，每条输出：
- summary: 事件摘要（能单独成立，不润色不编造）
- source_indices: 这条事件依据了【待切分】里的第几条（1 开始的序号数组）
- merge_into: 如果这条与【已有卡片】里的某条是同一件事，填那个 event_id；否则 null

规则：
- source_indices 只能填 1~{n}，**不许引用【上文】的序号**
- 如果【上文】仍不足以理解指代，输出 {{"needs_more_context": true}}
- 只输出 JSON 数组，不要解释

示例：
[{{"summary": "王泳完成前端页面修改", "source_indices": [2], "merge_into": null}}]"""


@dataclass
class SourcedEvent:
    """一条带来源的事件（spec 046）。"""

    summary: str
    source_indices: list[int] = field(default_factory=list)
    merge_into: str | None = None
    needs_more_context: bool = False


def split_with_sources(
    context: list[dict],
    window: list[dict],
    settings: Settings,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> list[SourcedEvent]:
    """带会话上文的切分，返回带源索引的事件。

    Args:
        context: 同会话更早的消息（只读，不建卡）。
        window: 本次要处理的消息（每条 `{text, ts}`）。
        settings: LLM 配置。

    Returns:
        事件列表；**至少一条**。LLM 失败/解析失败时降级为「一条消息一个事件」，
        这样时间和范围仍然精确，只是没有摘要润色。

    Note:
        越界索引在这里就被丢掉 —— 建卡只能落在本片，是 REQ-3 的硬约束。
    """
    window = [w for w in window if (w.get("text") or "").strip()]
    if not window:
        return []

    ctx_lines = [
        f"{i}. {c.get('text', '')}".replace("\n", " ")
        for i, c in enumerate(context, 1)
    ]
    win_lines = [
        f"{i}. {w.get('text', '')}".replace("\n", " ")
        for i, w in enumerate(window, 1)
    ]
    prompt_parts = []
    if ctx_lines:
        prompt_parts.append("【上文，仅供理解指代，不要为它建卡】\n" + "\n".join(ctx_lines))
    prompt_parts.append("【待切分，只处理这些】\n" + "\n".join(win_lines))
    prompt = "\n\n".join(prompt_parts)

    # 单条极短文本不值得一次 LLM 调用（与会话模式无关：一条「好的」不用切）
    _too_short = len(window) == 1 and len((window[0].get("text") or "")) <= 10
    raw: Any = []
    if not _too_short and len(prompt) > 20:
        _est = max(8192, min(16384, len(prompt) * 3))
        logger.info(
            "LLM 带上下文切分 | 上文 %d 条 | 本片 %d 条 | max_tokens=%d",
            len(context), len(window), _est,
        )
        try:
            raw = chat(
                [{"role": "user", "content": prompt}],
                settings,
                system=_SYSTEM_SOURCED.format(n=len(window)),
                max_tokens=_est,
                client=client,
                timeout_s=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - 降级：一条消息一个事件
            logger.warning("带上下文切分失败，降级为逐条: %s", exc)
            raw = []

    events = _parse_sourced(raw, len(window))
    if not events:
        # 降级：一条消息一个事件（时间/范围仍精确，只是没有摘要）
        events = [
            SourcedEvent(summary=(w.get("text") or "").strip(), source_indices=[i])
            for i, w in enumerate(window, 1)
        ]
    return events


def _parse_sourced(raw: Any, window_len: int) -> list[SourcedEvent]:
    """解析 LLM 输出；越界索引直接丢。"""
    if isinstance(raw, dict):
        if raw.get("needs_more_context"):
            return [SourcedEvent(summary="", needs_more_context=True)]
        raw = raw.get("events") or []
    if not isinstance(raw, list):
        return []

    out: list[SourcedEvent] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(SourcedEvent(summary=text))
            continue
        if not isinstance(item, dict):
            continue
        if item.get("needs_more_context"):
            return [SourcedEvent(summary="", needs_more_context=True)]
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        indices = [
            int(i) for i in (item.get("source_indices") or [])
            if isinstance(i, (int, float)) and 1 <= int(i) <= window_len
        ]
        merge_to = item.get("merge_into")
        out.append(SourcedEvent(
            summary=summary,
            source_indices=indices,
            merge_into=str(merge_to).strip() if merge_to else None,
        ))
    return out


def split_all(
    text: str,
    settings: Settings,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
    max_rounds: int = MAX_SPLIT_ROUNDS,
) -> SplitResult:
    """切完整段文本：超上限就**按行二分再来**，而不是截断（spec 036）。

    为什么二分而不是「一次切更多条」：`MAX_EVENTS` 防的是 LLM 把一段话
    切成几十条垃圾，**加高上限只是把阈值往后推** —— 遇到更大的批次一样撞。
    二分改变的是「一次喂给 LLM 多少输入」，上限本身不动，防滥用的性质保住了。

    分不动的情况（单行超长文本 / `max_rounds` 用尽）：截断到 `MAX_EVENTS`，
    丢掉的条数记进 `SplitResult.dropped`，由调用方打 WARNING。**绝不静默。**

    Args:
        text: 待切分的整段文本。
        max_rounds: 最多二分几轮（第一轮不含二分，故 `rounds=1` 表示没分过）。

    Returns:
        `SplitResult(parts, rounds, dropped)`；空输入是 `parts=[]`、`rounds=0`。
    """
    stripped = (text or "").strip()
    if not stripped:
        return SplitResult(parts=[], rounds=0, dropped=0)

    queue: list[tuple[str, int]] = [(stripped, 1)]
    parts: list[str] = []
    dropped = 0
    rounds = 1

    while queue:
        chunk, depth = queue.pop(0)
        got = _split_once(chunk, settings, client=client, timeout_s=timeout_s)

        # **严格大于**才二分：`== MAX_EVENTS` 是「刚好到上限」，一条没丢，
        # 不该为它多付一次 LLM 调用（A2「≤40 条行为不变」也靠这条）。
        if len(got) <= MAX_EVENTS:
            parts.extend(got)
            continue

        halves = _halve(chunk)
        if halves is not None and depth < max_rounds:
            rounds = max(rounds, depth + 1)
            queue[0:0] = [(h, depth + 1) for h in halves]
            logger.info(
                "切分撞上限 %d 条 → 二分重切（第 %d 轮）| 输入 %d 字",
                len(got),
                depth + 1,
                len(chunk),
            )
            continue

        # 到保护上限仍超限：截断 + 记账
        kept = min(len(got), MAX_EVENTS)
        parts.extend(got[:kept])
        dropped += len(got) - kept
        logger.warning(
            "切分到上限仍超限，尾部丢弃 %d 条 | kept=%d dropped=%d rounds=%d 输入=%d 字",
            len(got) - kept,
            kept,
            dropped,
            rounds,
            len(chunk),
        )

    return SplitResult(parts=parts, rounds=rounds, dropped=dropped)


def _halve(text: str) -> tuple[str, str] | None:
    """按行把文本对半分。切不动（只有一行 / 有一半为空）返回 `None`。

    只丢中间那个换行，行内内容原样保留 —— 二分是**分批**的手段，不是改写。
    """
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    mid = len(lines) // 2
    left = "\n".join(lines[:mid]).strip()
    right = "\n".join(lines[mid:]).strip()
    if not left or not right:
        return None
    return left, right


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
