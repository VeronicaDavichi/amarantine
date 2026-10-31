"""B2 事件切分的测试。"""

from __future__ import annotations

import json

import pytest

from memory.config import load_settings
from memory.services import llm_split as mod
from memory.services.llm import LLMError
from memory.services.llm_split import MAX_EVENT_CHARS, MAX_EVENTS, split_events


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


def test_too_many_events_falls_back(monkeypatch) -> None:
    """LLM 把一句话拆成几十条 → 判为异常，整段当一条。"""
    reply = json.dumps([f"事件{i}" for i in range(MAX_EVENTS + 5)])
    _patch(monkeypatch, reply=reply)
    assert split_events(LONG, SETTINGS) == [LONG.strip()]


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
