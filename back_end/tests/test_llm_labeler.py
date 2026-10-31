"""B4/B5 LLM 打标器与双跑对比的测试。

不碰真 LLM：注入假 `chat`（通过 `client` 参数不好注入，这里直接 monkeypatch 模块级 `chat`）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from memory.config import load_settings
from memory.models.event_card import Entity
from memory.services import llm_labeler as mod
from memory.services.labeler import LabelResult, label_by_rules
from memory.services.llm import LLMError
from memory.services.llm_labeler import (
    LabelComparison,
    compare_labels,
    jaccard,
    make_llm_labeler,
)


SETTINGS = load_settings()


def _patch_chat(monkeypatch, *, reply: str | None = None, exc: Exception | None = None):
    """把模块里的 chat 换成假实现。"""
    calls: list[dict[str, Any]] = []

    def fake(messages, settings, *, system=None, max_tokens=1024, client=None, timeout_s=None):
        calls.append({"messages": messages, "system": system, "max_tokens": max_tokens})
        if exc is not None:
            raise exc
        return reply or ""

    monkeypatch.setattr(mod, "chat", fake)
    return calls


# ---------------- jaccard ----------------


def test_jaccard_identical() -> None:
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0


def test_jaccard_disjoint() -> None:
    assert jaccard(["a"], ["b"]) == 0.0


def test_jaccard_partial() -> None:
    # 交集 1，并集 3
    assert jaccard(["a", "b"], ["a", "c"]) == pytest.approx(1 / 3)


def test_jaccard_both_empty_is_agree() -> None:
    """两边都没打出来算「一致」，不该送复核。"""
    assert jaccard([], []) == 1.0


def test_jaccard_one_empty_is_disagree() -> None:
    assert jaccard([], ["a"]) == 0.0
    assert jaccard(["a"], []) == 0.0


def test_jaccard_ignores_order_and_dups() -> None:
    assert jaccard(["b", "a", "a"], ["a", "b"]) == 1.0


# ---------------- compare_labels ----------------


def _res(types: list[str]) -> LabelResult:
    return LabelResult(types=types, entities=[], source="rules")


def test_compare_agree_no_review() -> None:
    c = compare_labels(_res(["work", "decision"]), _res(["work", "decision"]))
    assert c.agreement == 1.0
    assert c.needs_review is False


def test_compare_disagree_needs_review() -> None:
    c = compare_labels(_res(["work"]), _res(["health"]))
    assert c.agreement == 0.0
    assert c.needs_review is True
    assert c.only_rules == ["work"]
    assert c.only_llm == ["health"]


def test_compare_llm_none_never_reviews() -> None:
    """LLM 没跑或失败 → 不算不一致，不该骚扰人。"""
    c = compare_labels(_res(["work"]), None)
    assert c.agreement == 1.0
    assert c.needs_review is False


def test_compare_threshold_configurable() -> None:
    # 交集 1 / 并集 3 = 0.333
    c = compare_labels(_res(["a", "b"]), _res(["a", "c"]), agreement_min=0.3)
    assert c.needs_review is False
    c2 = compare_labels(_res(["a", "b"]), _res(["a", "c"]), agreement_min=0.5)
    assert c2.needs_review is True


def test_comparison_to_dict() -> None:
    c = compare_labels(_res(["work"]), _res(["health"]))
    d = c.to_dict()
    assert d["rules_types"] == ["work"]
    assert d["llm_types"] == ["health"]
    assert d["needs_review"] is True
    assert d["agreement"] == 0.0


# ---------------- LLM labeler：正常路径 ----------------


def test_llm_labeler_returns_types(monkeypatch) -> None:
    reply = json.dumps({"types": ["decision", "work"], "entities": []})
    _patch_chat(monkeypatch, reply=reply)
    labeler = make_llm_labeler(SETTINGS)
    out = labeler("张三决定项目A延期", ["work"])
    assert out is not None
    assert set(out.types) == {"decision", "work"}
    assert out.source == "llm"


def test_llm_labeler_strips_code_fence(monkeypatch) -> None:
    reply = '```json\n{"types": ["work"], "entities": []}\n```'
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("项目上线", [])
    assert out is not None and out.types == ["work"]


def test_llm_labeler_tolerates_prose_around_json(monkeypatch) -> None:
    reply = '好的，结果是：{"types": ["work"], "entities": []} 以上。'
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("项目上线", [])
    assert out is not None and out.types == ["work"]


def test_llm_labeler_parses_entities(monkeypatch) -> None:
    reply = json.dumps(
        {
            "types": ["work"],
            "entities": [
                {"name": "项目A", "kind": "topic"},
                {"name": "张三", "kind": "person"},
            ],
        }
    )
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("张三推进项目A", [])
    assert out is not None
    names = {e.name for e in out.entities}
    assert names == {"项目A", "张三"}
    kinds = {e.name: e.kind for e in out.entities}
    assert kinds["张三"] == "person"


def test_llm_labeler_illegal_kind_falls_back_to_topic(monkeypatch) -> None:
    """`Entity.kind` 是受控枚举，非法值要退回 topic 而不是丢掉实体。"""
    reply = json.dumps(
        {"types": ["work"], "entities": [{"name": "项目A", "kind": "spaceship"}]}
    )
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("项目A", [])
    assert out is not None
    assert out.entities == [Entity(name="项目A", kind="topic")]


# ---------------- 词表约束（防自由生成） ----------------


def test_llm_labeler_rejects_types_outside_vocab(monkeypatch) -> None:
    reply = json.dumps({"types": ["spaceship", "work"], "entities": []})
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("句子", [])
    assert out is not None
    assert out.types == ["work"]  # spaceship 被剔掉


def test_llm_labeler_all_outside_vocab_returns_none(monkeypatch) -> None:
    """全是不认识的标签 → 返回 None，让调用方回退规则层。"""
    reply = json.dumps({"types": ["spaceship", "dragon"], "entities": []})
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS)("句子", [])
    assert out is None


def test_llm_labeler_respects_custom_vocab(monkeypatch) -> None:
    reply = json.dumps({"types": ["work", "custom"], "entities": []})
    _patch_chat(monkeypatch, reply=reply)
    out = make_llm_labeler(SETTINGS, vocab=["custom"])("句子", [])
    assert out is not None
    assert out.types == ["custom"]


# ---------------- 失败降级 ----------------


def test_llm_labeler_swallows_llm_error(monkeypatch) -> None:
    _patch_chat(monkeypatch, exc=LLMError("网关挂了"))
    assert make_llm_labeler(SETTINGS)("句子", []) is None


def test_llm_labeler_swallows_unexpected_error(monkeypatch) -> None:
    _patch_chat(monkeypatch, exc=RuntimeError("boom"))
    assert make_llm_labeler(SETTINGS)("句子", []) is None


def test_llm_labeler_bad_json_returns_none(monkeypatch) -> None:
    _patch_chat(monkeypatch, reply="我拒绝输出 JSON")
    assert make_llm_labeler(SETTINGS)("句子", []) is None


def test_llm_labeler_non_list_types_returns_none(monkeypatch) -> None:
    _patch_chat(monkeypatch, reply=json.dumps({"types": "work"}))
    assert make_llm_labeler(SETTINGS)("句子", []) is None


def test_llm_labeler_empty_reply_returns_none(monkeypatch) -> None:
    _patch_chat(monkeypatch, reply="")
    assert make_llm_labeler(SETTINGS)("句子", []) is None


# ---------------- 与 label_event 集成 ----------------


def test_label_event_uses_llm_when_available(monkeypatch) -> None:
    from memory.services.labeler import label_event

    reply = json.dumps({"types": ["study"], "entities": []})
    _patch_chat(monkeypatch, reply=reply)
    out = label_event(
        "今天背了五十个单词", vocab=None, llm_labeler=make_llm_labeler(SETTINGS)
    )
    assert out.source == "llm"
    assert "study" in out.types


def test_label_event_falls_back_when_llm_fails(monkeypatch) -> None:
    from memory.services.labeler import label_event

    _patch_chat(monkeypatch, exc=LLMError("挂了"))
    out = label_event(
        "项目上线了", vocab=None, llm_labeler=make_llm_labeler(SETTINGS)
    )
    assert out.source == "rules"
    assert out.types  # 规则层保证非空


def test_prompt_includes_vocab_and_rule_hint(monkeypatch) -> None:
    calls = _patch_chat(
        monkeypatch, reply=json.dumps({"types": ["work"], "entities": []})
    )
    make_llm_labeler(SETTINGS)("项目上线", ["work"])
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert "work" in content          # 词表进了 prompt
    assert "项目上线" in content       # 原文进了 prompt
    assert calls[0]["system"]         # system prompt 有传
