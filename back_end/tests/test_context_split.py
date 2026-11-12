"""spec 046：带上下文切分（源索引 / 归并 / 上文不足）。"""

from __future__ import annotations

import pytest

from memory.services import llm_split as LS
from memory.services.raw_quote import _event_time, _min_ts


def _win(texts):
    return [{"text": t, "ts": f"2026-05-0{i+1} 10:00:00"} for i, t in enumerate(texts)]


def test_prompt_contains_context_and_readonly_warning(monkeypatch) -> None:
    """REQ-1：上文进提示词，且明确「不要为它建卡」。"""
    captured = {}

    def fake_chat(messages, settings, **kw):
        captured["user"] = messages[0]["content"]
        captured["system"] = kw.get("system")
        return [{"summary": "王泳完成前端修改", "source_indices": [1], "merge_into": None}]

    monkeypatch.setattr(LS, "chat", fake_chat)
    ctx = [{"text": "王泳：那个页面我改完了", "ts": "2026-04-30 10:00:00"}]
    events = LS.split_with_sources(ctx, _win(["王泳：前端页面已经改完并部署到测试环境了"]), object())

    assert "【上文" in captured["user"]
    assert "不要为它建卡" in captured["system"] + captured["user"]
    assert events[0].summary == "王泳完成前端修改"
    assert events[0].source_indices == [1]


def test_out_of_range_indices_dropped(monkeypatch) -> None:
    """REQ-3：越界索引丢弃（建卡只能落在本片）。"""
    monkeypatch.setattr(LS, "chat", lambda *a, **kw: [
        {"summary": "混入上文的事件", "source_indices": [9]},
        {"summary": "正常事件", "source_indices": [2]},
    ])
    events = LS.split_with_sources([], _win(["这是第一句比较长的话", "这是第二句比较长的话"]), object())
    assert [e.summary for e in events] == ["混入上文的事件", "正常事件"]
    assert events[0].source_indices == []
    assert events[1].source_indices == [2]


def test_merge_into_parsed(monkeypatch) -> None:
    """REQ-4：同一事件 → merge_into 带上旧卡 id。"""
    monkeypatch.setattr(LS, "chat", lambda *a, **kw: [
        {"summary": "同一件事的补充", "source_indices": [1], "merge_into": "evt_old123"},
    ])
    events = LS.split_with_sources([], _win(["再补充一句关于页面的事"]), object())
    assert events[0].merge_into == "evt_old123"


def test_needs_more_context_parsed(monkeypatch) -> None:
    """REQ-2：模型说上文不够时给出信号（run_ingest 会拉 120 条重试）。"""
    monkeypatch.setattr(LS, "chat", lambda *a, **kw: {"needs_more_context": True})
    events = LS.split_with_sources([], _win(["那个蓝色的键帽到底要多少钱啊"]), object())
    assert events and events[0].needs_more_context


def test_llm_failure_falls_back_per_turn(monkeypatch) -> None:
    """降级：一条消息一个事件（时间与范围仍精确）。"""

    def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(LS, "chat", boom)
    events = LS.split_with_sources([], _win(["第一句比较长的话内容", "第二句比较长的话内容"]), object())
    assert [e.summary for e in events] == ["第一句比较长的话内容", "第二句比较长的话内容"]
    assert [e.source_indices for e in events] == [[1], [2]]


# ------------------------------------------------------------ 时间映射


def test_event_time_uses_source_indices() -> None:
    """REQ-4（时间）：事件时间 = 源消息里最早的 ts。"""
    from memory.services.llm_split import SourcedEvent

    turns = [
        {"text": "a", "ts": "2026-05-01 10:00:00"},
        {"text": "b", "ts": "2026-05-03 09:00:00"},
    ]
    event = SourcedEvent(summary="x", source_indices=[2, 1])
    got = _event_time(event, turns, None)
    assert got is not None and got.day == 1


def test_event_time_falls_back_to_4gram(monkeypatch) -> None:
    from memory.services.llm_split import SourcedEvent

    turns = [{"text": "a", "ts": "2026-05-01 10:00:00"}]
    event = SourcedEvent(summary="无关的话", source_indices=[])
    got = _event_time(event, turns, "2026-05-02")
    assert got is not None and got.day == 2


def test_min_ts() -> None:
    turns = [{"ts": "2026-05-03"}, {"ts": "2026-05-01"}, {"ts": None}]
    assert _min_ts(turns) == "2026-05-01"
    assert _min_ts([]) is None
