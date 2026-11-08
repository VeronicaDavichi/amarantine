"""B2 事件切分的测试。"""

from __future__ import annotations

import json
import logging

import pytest

from memory.config import load_settings
from memory.services import llm_split as mod
from memory.services.llm import LLMError
from memory.services.llm_split import (
    MAX_EVENT_CHARS,
    MAX_EVENTS,
    MAX_SPLIT_ROUNDS,
    split_all,
    split_events,
)


SETTINGS = load_settings()

#: 超过 40 字符才会真去调 LLM
LONG = "昨天跟张三开会定了项目A延期到六月，然后我去体检，医生说血压有点高，让我少吃盐多运动"


def _patch(monkeypatch, *, reply: str | None = None, exc: Exception | None = None):
    calls: list[dict] = []

    def fake(messages, settings, *, system=None, max_tokens=1024, client=None, timeout_s=None):
        calls.append({"messages": messages, "system": system})
        if exc is not None:
            raise exc
        return reply or ""

    monkeypatch.setattr(mod, "chat", fake)
    return calls


# ---------------- 正常切分 ----------------


def test_splits_multiple_events(monkeypatch) -> None:
    reply = json.dumps(
        ["张三决定项目A延期到六月", "体检发现血压偏高", "医生建议少吃盐多运动"]
    )
    _patch(monkeypatch, reply=reply)
    parts = split_events(LONG, SETTINGS)
    assert len(parts) == 3
    assert parts[0] == "张三决定项目A延期到六月"


def test_single_event_stays_one(monkeypatch) -> None:
    _patch(monkeypatch, reply=json.dumps(["就只有一件事"]))
    assert split_events(LONG, SETTINGS) == ["就只有一件事"]


def test_strips_code_fence(monkeypatch) -> None:
    _patch(monkeypatch, reply='```json\n["事件一", "事件二"]\n```')
    assert split_events(LONG, SETTINGS) == ["事件一", "事件二"]


def test_tolerates_prose(monkeypatch) -> None:
    _patch(monkeypatch, reply='好的，拆分结果：["事件一", "事件二"] 以上。')
    assert split_events(LONG, SETTINGS) == ["事件一", "事件二"]


def test_accepts_wrapped_object(monkeypatch) -> None:
    """也接受 {"events": [...]} 这种包了一层的输出。"""
    _patch(monkeypatch, reply=json.dumps({"events": ["事件一", "事件二"]}))
    assert split_events(LONG, SETTINGS) == ["事件一", "事件二"]


def test_accepts_list_of_objects(monkeypatch) -> None:
    _patch(monkeypatch, reply=json.dumps([{"summary": "事件一"}, {"summary": "事件二"}]))
    assert split_events(LONG, SETTINGS) == ["事件一", "事件二"]


def test_drops_blank_entries(monkeypatch) -> None:
    _patch(monkeypatch, reply=json.dumps(["事件一", "  ", "", "事件二"]))
    assert split_events(LONG, SETTINGS) == ["事件一", "事件二"]


# ---------------- 短路：不该调 LLM 的情况 ----------------


def test_short_text_skips_llm(monkeypatch) -> None:
    calls = _patch(monkeypatch, reply='["不该被调用"]')
    out = split_events("项目A延期了", SETTINGS)
    assert out == ["项目A延期了"]
    assert calls == []


def test_empty_text(monkeypatch) -> None:
    _patch(monkeypatch, reply="[]")
    assert split_events("", SETTINGS) == []
    assert split_events("   ", SETTINGS) == []


def test_newline_forces_llm_even_if_short(monkeypatch) -> None:
    """短但多行的输入也要拆（用户可能用换行列了几件事）。"""
    calls = _patch(monkeypatch, reply=json.dumps(["事件一", "事件二"]))
    out = split_events("事件一\n事件二", SETTINGS)
    assert len(calls) == 1
    assert out == ["事件一", "事件二"]


# ---------------- 降级 ----------------


def test_llm_error_returns_whole_text(monkeypatch) -> None:
    _patch(monkeypatch, exc=LLMError("网关挂了"))
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_unexpected_error_returns_whole_text(monkeypatch) -> None:
    _patch(monkeypatch, exc=RuntimeError("boom"))
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_bad_json_returns_whole_text(monkeypatch) -> None:
    _patch(monkeypatch, reply="我不输出 JSON")
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_empty_reply_returns_whole_text(monkeypatch) -> None:
    _patch(monkeypatch, reply="")
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_empty_array_returns_whole_text(monkeypatch) -> None:
    _patch(monkeypatch, reply="[]")
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_echo_single_part_is_accepted(monkeypatch) -> None:
    """只返回原文一条 —— 这是"没拆"，不是失败，可以接受。"""
    _patch(monkeypatch, reply=json.dumps([LONG]))
    assert split_events(LONG, SETTINGS) == [LONG]


# ---------------- 防滥用护栏 ----------------


def test_too_many_events_truncates_not_falls_back(monkeypatch) -> None:
    """超过 `MAX_EVENTS` → **截断**，不是整段丢弃。

    ⚠️ **这个测试在 spec 034 被改过**，改的是行为不是断言强度。

    **原来断言**：「拆成几十条 → 判异常 → 整段当一条」。

    **为什么改**：实测（DingTalk 32 轮日常对话）LLM 切出 **46 条**，
    平均每轮 1.4 条 —— 完全合理。原逻辑 `_looks_like_echo` 看到
    `len(parts) > MAX_EVENTS` 就返回 True，于是整段丢弃，产出 1 张
    `summary` 是原文拼接的垃圾卡，**而且不报错**（设计上就是降级）。

    更糟的是那行 `return parts[:MAX_EVENTS]` 明明写着截断，
    因为前面的判断先返回，**永远执行不到** —— 两个逻辑互相打架。

    **现在的行为**：`_looks_like_echo` 只管「质量」（原文照抄 / 单条过长），
    「条数」由 `parts[:MAX_EVENTS]` 截断。一段长对话保留前 40 条摘要，
    远好于整段丢弃。
    """
    reply = json.dumps([f"事件{i}" for i in range(MAX_EVENTS + 5)])
    _patch(monkeypatch, reply=reply)
    parts = split_events(LONG, SETTINGS)
    assert parts != [LONG.strip()], "不该整段丢弃，应该截断"
    assert len(parts) == MAX_EVENTS, f"应该截断到 {MAX_EVENTS}，实际 {len(parts)}"


def test_overly_long_event_falls_back(monkeypatch) -> None:
    """某条超长 → 说明没真拆开，整段当一条。"""
    reply = json.dumps(["事件一", "x" * (MAX_EVENT_CHARS + 10)])
    _patch(monkeypatch, reply=reply)
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


def test_truncates_to_max(monkeypatch) -> None:
    reply = json.dumps([f"事件{i}" for i in range(MAX_EVENTS)])
    _patch(monkeypatch, reply=reply)
    parts = split_events(LONG, SETTINGS)
    assert len(parts) == MAX_EVENTS


# ---------------- prompt 内容 ----------------


def test_prompt_contains_text(monkeypatch) -> None:
    calls = _patch(monkeypatch, reply=json.dumps(["一"]))
    split_events(LONG, SETTINGS)
    assert LONG in calls[0]["messages"][0]["content"]
    assert calls[0]["system"]


# ---------------- split_all：超限二分（spec 036） ----------------


def _patch_echo_lines(monkeypatch, *, per_call: int | None = None):
    """假 LLM：把送进去的每一行原样当一条事件吐回来。

    `per_call` 给定时无视输入、固定返回 N 条 —— 用来模拟「LLM 就是切得多」。
    """
    calls: list[dict] = []

    def fake(messages, settings, *, system=None, max_tokens=1024, client=None, timeout_s=None):
        calls.append({"messages": messages})
        if per_call is not None:
            return json.dumps([f"事件{i}" for i in range(per_call)])
        body = messages[0]["content"].split("\n", 1)[1]
        return json.dumps([line.strip() for line in body.splitlines() if line.strip()])

    monkeypatch.setattr(mod, "chat", fake)
    return calls


def _lines(n: int) -> str:
    return "\n".join(f"第{i}件事" for i in range(n))


def test_split_all_returns_every_event_for_65(monkeypatch) -> None:
    """036 A1：65 条输入 → **65 条全落**，靠二分，dropped=0。

    这是本 spec 的核心断言：老接口 `split_events` 同样输入只给 40 条。
    """
    calls = _patch_echo_lines(monkeypatch)
    text = _lines(65)

    result = split_all(text, SETTINGS)

    assert len(result.parts) == 65
    assert result.dropped == 0
    assert result.rounds == 2, "65 > 40，应该二分一次"
    # 1 次全量 + 2 次半量 = 3 次调用
    assert len(calls) == 3


def test_split_events_still_truncates(monkeypatch) -> None:
    """不变行为：老接口仍是截断语义（单条写入路径没被这次改动波及）。"""
    _patch_echo_lines(monkeypatch)
    assert len(split_events(_lines(65), SETTINGS)) == MAX_EVENTS


def test_split_all_does_not_bisect_at_exactly_max(monkeypatch) -> None:
    """边界：刚好 40 条 → 不二分、不多付一次 LLM 调用。

    「== 上限」一条没丢，为它重切会给 A2（≤40 条结果不变）制造无谓差异。
    """
    calls = _patch_echo_lines(monkeypatch, per_call=MAX_EVENTS)

    result = split_all(_lines(65), SETTINGS)

    assert len(result.parts) == MAX_EVENTS
    assert result.rounds == 1
    assert result.dropped == 0
    assert len(calls) == 1


def test_split_all_records_dropped_when_unsplittable(monkeypatch, caplog) -> None:
    """036 A3：单行超长文本分不动 → 截断 + dropped>0 + WARNING。"""
    _patch_echo_lines(monkeypatch, per_call=MAX_EVENTS + 25)

    with caplog.at_level(logging.WARNING, logger="memory.llm_split"):
        result = split_all(LONG * 3, SETTINGS)  # 单行，没有可切的行边界

    assert len(result.parts) == MAX_EVENTS
    assert result.dropped == 25
    # 一行都切不动 → 一轮都没二分，rounds 保持 1
    assert result.rounds == 1
    assert any("丢弃" in r.getMessage() for r in caplog.records)


def test_split_all_stops_at_max_rounds(monkeypatch) -> None:
    """轮次用尽也算「分不动」：截断 + dropped，不许无限重切。

    100 行、假 LLM 每次固定 65 条 → 层层二分到 3 轮，最后 4 段各截断到 40。
    """
    _patch_echo_lines(monkeypatch, per_call=MAX_EVENTS + 25)

    result = split_all(_lines(100), SETTINGS)

    assert result.rounds == MAX_SPLIT_ROUNDS == 3
    assert len(result.parts) == 4 * MAX_EVENTS
    assert result.dropped == 4 * 25


def test_split_all_degrades_like_split_events(monkeypatch) -> None:
    """不变行为：LLM 抛错 → 整段一条，rounds=1，不抛。"""
    _patch(monkeypatch, exc=LLMError("网关超时"))
    result = split_all(LONG, SETTINGS)
    assert result.parts == [LONG.strip()]
    assert result.rounds == 1
    assert result.dropped == 0


def test_split_all_empty_input() -> None:
    assert split_all("", SETTINGS).parts == []
    assert split_all("   ", SETTINGS).rounds == 0

def test_split_all_matches_split_events_under_limit(monkeypatch) -> None:
    """036 A2：≤40 条时两个入口必须给出**逐条相同**的结果。

    「与改动前逐字节一致」没法跑旧代码，但可以钉一个更强的性质：
    没撞上限时，`split_all` 与 `split_events` 走的是同一条路径 ——
    如果哪天断言不了这条，才说明二分逻辑偷偷影响了短批次。
    """
    _patch_echo_lines(monkeypatch)
    text = _lines(30)

    assert split_all(text, SETTINGS).parts == split_events(text, SETTINGS)
    assert split_all(text, SETTINGS).rounds == 1
