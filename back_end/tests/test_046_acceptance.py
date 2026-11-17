"""spec 046 A3/A5：越界索引不建卡 + 已有卡注入使 merge_into 真正可用。"""

from __future__ import annotations

import pytest

from memory.services import llm_split as LS


def _win(texts):
    return [{"text": t, "ts": f"2026-05-0{i+1} 10:00:00"} for i, t in enumerate(texts)]


def test_out_of_range_indices_marked_invalid() -> None:
    """A3：给了索引但全越界 → invalid_source，调用方不建卡。"""
    events = LS._parse_sourced(
        [{"summary": "越界事件", "source_indices": [99]}], 3)
    assert len(events) == 1
    assert events[0].invalid_source is True
    assert events[0].source_indices == []


def test_mixed_indices_keep_valid_part() -> None:
    """部分越界：保留合法部分，不算 invalid。"""
    events = LS._parse_sourced(
        [{"summary": "半越界", "source_indices": [2, 99]}], 3)
    assert events[0].source_indices == [2]
    assert events[0].invalid_source is False


def test_no_indices_is_not_invalid() -> None:
    """没给索引 ≠ 越界：走 4-gram 兜底，仍建卡。"""
    events = LS._parse_sourced([{"summary": "无索引事件"}], 3)
    assert events[0].source_indices == []
    assert events[0].invalid_source is False


def test_merge_into_whitelisted() -> None:
    """A5：merge_into 只能指向真实存在的已有卡，编的 id 被丢弃。"""
    events = LS._parse_sourced(
        [{"summary": "a", "source_indices": [1], "merge_into": "evt_fake"}],
        3, allowed_ids={"evt_real"},
    )
    assert events[0].merge_into is None

    events2 = LS._parse_sourced(
        [{"summary": "b", "source_indices": [1], "merge_into": "evt_real"}],
        3, allowed_ids={"evt_real"},
    )
    assert events2[0].merge_into == "evt_real"


def test_prompt_contains_existing_cards(monkeypatch) -> None:
    """A5 根因修复：提示词必须真的带上已有卡，否则 merge_into 永远为 null。"""
    captured = {}

    def fake_chat(messages, settings, **kw):
        captured["system"] = kw.get("system") or ""
        return [{"summary": "x", "source_indices": [1], "merge_into": "evt_old1"}]

    monkeypatch.setattr(LS, "chat", fake_chat)
    events = LS.split_with_sources(
        [], _win(["这是一句明显超过十个字的测试句子"]), object(),
        existing=[{"event_id": "evt_old1", "summary": "旧卡摘要"}],
    )
    assert "evt_old1" in captured["system"], captured["system"][:400]
    assert "旧卡摘要" in captured["system"]
    assert events[0].merge_into == "evt_old1"


def test_prompt_without_existing_says_none(monkeypatch) -> None:
    captured = {}

    def fake_chat(messages, settings, **kw):
        captured["system"] = kw.get("system") or ""
        return [{"summary": "x", "source_indices": [1]}]

    monkeypatch.setattr(LS, "chat", fake_chat)
    LS.split_with_sources([], _win(["这是一句明显超过十个字的测试句子"]), object())
    assert "（无）" in captured["system"]
