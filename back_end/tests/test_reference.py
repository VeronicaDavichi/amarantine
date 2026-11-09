"""spec 041：指称层（触发打分 / 阈值 / 多义 / 三档查询）。

覆盖 A1（称呼位建候选）、A2（食物语境不建）、A4（多义共存）、
A5（scope 隔离）、A6（tier 过滤）、A8（弱边不传递是设计约束，单测覆盖 pick_sense）。
"""

from __future__ import annotations

import pytest

from memory.services import reference as R


# ------------------------------------------------------------ A1/A2 触发信号


def test_at_mention_scores() -> None:
    """A1：`@鱼 帮我查下` → 鱼 得 +3。"""
    sigs = R.extract_signals("@鱼 帮我查下这个接口")
    assert any(s.surface == "鱼" and s.weight == 3 and s.kind == "at" for s in sigs)


def test_speak_position_scores() -> None:
    """`鱼说` / `问鱼` / `鱼在吗` 都是称呼位（`问鱼` 走前置动词形态）。"""
    for text in ("鱼说这个能行", "问鱼一下", "鱼在吗"):
        sigs = R.extract_signals(text)
        assert any(
            s.surface == "鱼" and s.kind in ("speak", "call") for s in sigs
        ), text


def test_define_sentence_gives_gloss() -> None:
    """定义句 `鱼就是那个 bot` → +3 且带 gloss。"""
    sigs = R.extract_signals("鱼就是那个 deepseek 的 bot")
    define = [s for s in sigs if s.kind == "define"]
    assert define and define[0].gloss


def test_negative_signal_for_food_context() -> None:
    """A2：`这鱼新鲜吗` → 反向信号 −2，不足以建候选。"""
    sigs = R.extract_signals("这鱼新鲜吗")
    assert any(s.weight < 0 for s in sigs)
    cands = R.build_candidates(sigs, scope="session:x")
    assert cands == []


def test_stop_words_not_extracted() -> None:
    """代词/虚词不会被当成可指称名词。"""
    for text in ("他说这个可以", "我就是想问问", "这个是什么"):
        sigs = R.extract_signals(text)
        assert all(s.surface not in R._SURFACE_STOP for s in sigs)


# ------------------------------------------------------------ 阈值与状态


def test_score_below_threshold_no_candidate() -> None:
    """低于 2 分不建候选。"""
    sigs = R.extract_signals("鱼在吗")  # 只有 +3？改成构造 1 分场景
    # 直接构造低分信号
    low = [R.Signal("鱼", 1, "at")]
    assert R.build_candidates(low, scope="session:x", candidate_min=2) == []
    assert R.build_candidates(sigs, scope="session:x", candidate_min=2)


def test_score_at_4_becomes_probable() -> None:
    """≥4 且无矛盾 → probable。"""
    sigs = [
        R.Signal("鱼", 3, "at", quote_id="q1"),
        R.Signal("鱼", 3, "speak", quote_id="q2"),
    ]
    cands = R.build_candidates(sigs, scope="session:x")
    assert len(cands) == 1
    assert cands[0].status == "probable"
    assert cands[0].score == 6


def test_account_signal_boosts() -> None:
    """群内存在同名账号：昵称含字面 → +2。"""
    sigs = [R.Signal("鱼", 3, "at")]
    plain = R.build_candidates(sigs, scope="session:x")[0]
    boosted = R.build_candidates(sigs, scope="session:x", nicks=["xxxx的大肥鱼"])[0]
    assert boosted.score == plain.score + 2


# ------------------------------------------------------------ A4 多义共存


def test_multiple_senses_coexist() -> None:
    """A4：同一 Term 同 scope 可有两个义项（bot / 动物）。"""
    sigs = [
        R.Signal("鱼", 3, "define", gloss="deepseek agent"),
        R.Signal("鱼", 3, "define", gloss="动物"),
    ]
    cands = R.build_candidates(sigs, scope="session:x")
    assert {c.gloss for c in cands} == {"deepseek agent", "动物"}


def test_pick_sense_prefers_higher_confidence() -> None:
    cands = [
        {"term": "鱼", "gloss": "bot", "status": "probable", "confidence": 0.9, "score": 6},
        {"term": "鱼", "gloss": "动物", "status": "probable", "confidence": 0.6, "score": 3},
    ]
    sense, ambiguous = R.pick_sense(cands, tier="default")
    assert sense["gloss"] == "bot"
    assert ambiguous is True  # 两个都可选 → 标歧义


def test_pick_sense_unresolved_when_too_many() -> None:
    """超过 2 个可选义项 → 不猜（unresolved）。"""
    cands = [
        {"term": "ds", "gloss": g, "status": "probable", "confidence": 0.7, "score": 4}
        for g in ("deepseek", "大神", "导数")
    ]
    sense, ambiguous = R.pick_sense(cands, tier="default")
    assert sense is None
    assert ambiguous is True


# ------------------------------------------------------------ A6 三档查询


def test_tier_filters() -> None:
    """strict 只认 exact；default 含 probable；explore 全含。"""
    assert R.tier_allows("exact", "strict")
    assert not R.tier_allows("probable", "strict")
    assert R.tier_allows("probable", "default")
    assert not R.tier_allows("uncertain", "default")
    assert R.tier_allows("uncertain", "explore")


def test_tier_unknown_raises() -> None:
    with pytest.raises(ValueError):
        R.tier_allows("exact", "whatever")


def test_denied_not_in_strict_but_in_explore() -> None:
    """A7：人否认后 strict/default 不再命中，explore 仍可见。"""
    assert not R.tier_allows("denied", "strict")
    assert not R.tier_allows("denied", "default")
    assert R.tier_allows("denied", "explore")


# ------------------------------------------------------------ scope 隔离（A5）


def test_scope_is_part_of_candidate_key() -> None:
    """A5：同一字面在不同 scope 是不同候选。"""
    sigs_a = [R.Signal("鱼", 3, "at", quote_id="q1")]
    sigs_b = [R.Signal("鱼", 3, "at", quote_id="q2")]
    ca = R.build_candidates(sigs_a, scope="session:A")[0]
    cb = R.build_candidates(sigs_b, scope="session:B")[0]
    assert ca.scope != cb.scope
    assert ca.term == cb.term
