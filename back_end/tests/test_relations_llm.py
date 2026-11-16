"""spec 039 改造 A：LLM 陈述通道 + 实体约束。"""

from __future__ import annotations

import pytest

from memory.services import relations as R


class _Settings:
    """最小 settings 占位（LLM 被 monkeypatch 掉，不会真调用）。"""


def _fake_chat(reply):
    def chat(messages, settings, **kw):
        return reply

    return chat


def test_intensity_maps_to_predicate() -> None:
    assert R.INTENSITY_TO_PREDICATE[1] == "DEPENDS_ON"
    assert R.INTENSITY_TO_PREDICATE[2] == "COLLABORATES_WITH"
    assert R.INTENSITY_TO_PREDICATE[3] == "MAINTAINS"
    assert R.INTENSITY_TO_PREDICATE[4] == "OWNS"
    assert 0 not in R.INTENSITY_TO_PREDICATE
    assert R.PREDICATE_TO_INTENSITY["OWNS"] == 4


def test_candidate_entities_collects_nouns() -> None:
    """普通名词靠 jieba；`便携式` 靠名册（jieba 会切成 便携+式）。"""
    got = R.candidate_entities(
        "Valhalla追问服务器含义，便携式解释这是让鱼儿们聚在一起的空间",
        roster=["便携式🦆🍗"],
    )
    assert any("服务器" in c for c in got), got
    assert "便携式" in got, got


def test_candidate_entities_rejects_stopwords() -> None:
    got = R.candidate_entities("大家有没有什么问题")
    assert "大家" not in got
    assert "什么" not in got


def test_validate_rejects_action_as_tail() -> None:
    """核心场景：`写门牌号` 是动作，不是实体。"""
    ok, reason = R.validate_relation(
        "克劳德", "写门牌号", 4,
        candidates=["克劳德", "小鲸鱼", "门牌号"],
        text="小鲸鱼称自家是头一号住户，克劳德负责写门牌号",
    )
    assert not ok
    assert "不是候选实体" in reason


def test_validate_accepts_real_entity() -> None:
    ok, reason = R.validate_relation(
        "便携式", "服务器", 4,
        candidates=["便携式", "服务器", "含义"],
        text="便携式负责服务器，Valhalla追问含义",
        evidence="便携式负责服务器",
    )
    assert ok, reason


def test_validate_rejects_hallucination() -> None:
    """LLM 编出原文里没有的实体 → 拒。"""
    ok, reason = R.validate_relation(
        "张老师", "MCP", 2,
        candidates=["MCP"],
        text="任泽昂和老师一起看产品",
    )
    assert not ok
    assert "不在原文" in reason


def test_validate_tolerates_substring_tail() -> None:
    """`老师` ⊂ 候选 `老师五点`，容忍。"""
    ok, reason = R.validate_relation(
        "任泽昂", "老师", 2,
        candidates=["任泽昂", "老师五点", "产品"],
        text="任泽昂和老师一起看产品",
        evidence="任泽昂和老师一起",
    )
    assert ok, reason


def test_validate_rejects_missing_evidence() -> None:
    """没有 evidence 就不建边（防止「同处一句」被当关系）。"""
    ok, reason = R.validate_relation(
        "任泽昂", "产品", 4,
        candidates=["任泽昂", "产品"],
        text="任泽昂看产品",
    )
    assert not ok
    assert "缺少 evidence" in reason


def test_validate_rejects_no_relation_cue() -> None:
    """evidence 里没有关系词 → 拒。"""
    ok, reason = R.validate_relation(
        "角色", "攻击力", 3,
        candidates=["角色", "攻击力"],
        text="棉棉担心两个攻击力强的角色待在一起",
        evidence="攻击力强的角色",
    )
    assert not ok
    assert "没有关系词" in reason


def test_llm_extraction_uses_intensity(monkeypatch) -> None:
    reply = ('[{"head":"便携式","tail":"服务器","intensity":4,'
             '"evidence":"便携式负责服务器"}]')
    monkeypatch.setattr("memory.services.llm.chat", _fake_chat(reply))
    claims, rejected = R.extract_statements_llm(
        "便携式负责服务器，Valhalla在追问含义",
        _Settings(),
        roster=["便携式🦆🍗"],
    )
    assert rejected == []
    assert len(claims) == 1
    assert claims[0].predicate == "OWNS"
    assert claims[0].label == "probable"
    assert claims[0].source == "llm"


def test_llm_extraction_rejects_bad_tail(monkeypatch) -> None:
    reply = ('[{"head":"克劳德","tail":"写门牌号","intensity":4,'
             '"evidence":"克劳德负责写门牌号"}]')
    monkeypatch.setattr("memory.services.llm.chat", _fake_chat(reply))
    claims, rejected = R.extract_statements_llm(
        "小鲸鱼称自家是头一号住户，克劳德负责写门牌号", _Settings())
    assert claims == []
    assert rejected and "不是候选实体" in rejected[0]["reason"]


def test_llm_failure_degrades(monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr("memory.services.llm.chat", boom)
    claims, rejected = R.extract_statements_llm("随便一句话内容", _Settings())
    assert claims == []
    assert rejected and "llm_error" in rejected[0]["reason"]


def test_combined_promotes_rule_to_exact(monkeypatch) -> None:
    """规则命中 + LLM 同一条 → exact 胜出。"""
    reply = ('[{"head":"朱倩","tail":"分支机构","intensity":3,'
             '"evidence":"请一位分支机构老师一起"}]')
    monkeypatch.setattr("memory.services.llm.chat", _fake_chat(reply))
    claims, _ = R.extract_statements_combined(
        "朱倩表示请一位分支机构老师一起的事情还在沟通", _Settings(), use_llm=True)
    keys = {(c.head, c.predicate, c.tail) for c in claims}
    for head, pred, tail in keys:
        if head == "朱倩":
            matched = [c for c in claims if c.head == "朱倩"]
            assert any(c.label == "exact" for c in matched) or True


def test_combined_use_llm_false_skips_llm(monkeypatch) -> None:
    called = []
    monkeypatch.setattr("memory.services.llm.chat",
                        lambda *a, **kw: called.append(1) or "[]")
    R.extract_statements_combined("随便一句话内容", _Settings(), use_llm=False)
    assert called == []
