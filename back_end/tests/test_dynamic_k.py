"""C4 动态 top-K 决策单测。

六步规则的每一步都要能单独触发，且归因（reason）要准确——
`trace.top_k_decision` 是排查召回问题的唯一线索，写错了等于黑箱。
"""

from __future__ import annotations

import pytest

from memory.core.dynamic_k import Candidate, decide_k

BASE = dict(
    budget_tokens=10_000,
    min_k=1,
    max_k=10,
    min_score=0.0,
    drop_ratio=0.0,
    gap_ratio=1.0,
)


def _c(eid: str, score: float, tokens: int = 100, forced: bool = False) -> Candidate:
    return Candidate(event_id=eid, score=score, tokens=tokens, forced=forced)


# ---------------- 基础 ----------------


def test_empty_candidates() -> None:
    d = decide_k([], **BASE)
    assert d.k == 0 and d.reason == "empty" and d.selected == []


def test_all_kept_when_no_rule_triggers() -> None:
    d = decide_k([_c("a", 0.9), _c("b", 0.8)], **BASE)
    assert d.selected == ["a", "b"]
    assert d.reason == "all"
    assert not d.overflow


def test_results_are_sorted_by_score() -> None:
    d = decide_k([_c("low", 0.1), _c("high", 0.9)], **BASE)
    assert d.selected == ["high", "low"]


# ---------------- 第 2 步：绝对过滤 ----------------


def test_min_score_filters_weak() -> None:
    d = decide_k(
        [_c("good", 0.9), _c("weak", 0.05)],
        **{**BASE, "min_score": 0.1},
    )
    assert d.selected == ["good"]
    assert d.dropped_min_score == 1


def test_all_below_min_score_yields_empty() -> None:
    d = decide_k([_c("a", 0.01)], **{**BASE, "min_score": 0.5})
    assert d.selected == []
    assert d.reason == "min_score"


# ---------------- 第 3 步：相对衰减 ----------------


def test_drop_ratio_cuts_relative_low() -> None:
    """最高分 1.0，ratio=0.5 → 低于 0.5 的丢弃。"""
    d = decide_k(
        [_c("top", 1.0), _c("mid", 0.6), _c("low", 0.2)],
        **{**BASE, "drop_ratio": 0.5},
    )
    assert d.selected == ["top", "mid"]
    assert d.dropped_drop_ratio == 1


def test_drop_ratio_uses_top_after_min_filter() -> None:
    """相对阈值的基准是**过滤后的最高分**，不是原始最高分。"""
    d = decide_k(
        [_c("weak", 0.5), _c("top", 0.9)],
        **{**BASE, "min_score": 0.56, "drop_ratio": 0.9},
    )
    assert d.selected == ["top"]


# ---------------- 第 4 步：断崖 ----------------


def test_gap_detection_truncates_after_cliff() -> None:
    """0.9 → 0.85 → 0.3：0.85→0.3 降幅 65% 超过 gap_ratio=0.4，在此截断。"""
    d = decide_k(
        [_c("a", 0.9), _c("b", 0.85), _c("c", 0.3), _c("d", 0.28)],
        **{**BASE, "gap_ratio": 0.4},
    )
    assert d.selected == ["a", "b"]
    assert d.reason == "gap"
    assert d.dropped_gap == 2


def test_no_gap_when_scores_smooth() -> None:
    d = decide_k(
        [_c("a", 1.0), _c("b", 0.95), _c("c", 0.9)],
        **{**BASE, "gap_ratio": 0.4},
    )
    assert d.dropped_gap == 0


# ---------------- 第 5 步：预算 ----------------


def test_budget_truncates_by_tokens() -> None:
    d = decide_k(
        [_c("a", 0.9, tokens=400), _c("b", 0.8, tokens=400), _c("c", 0.7, tokens=400)],
        **{**BASE, "budget_tokens": 900},
    )
    assert d.selected == ["a", "b"]
    assert d.reason == "budget"
    assert d.dropped_budget == 1


def test_zero_budget_keeps_nothing_unless_forced() -> None:
    d = decide_k([_c("a", 0.9)], **{**BASE, "budget_tokens": 0})
    assert d.selected == []


# ---------------- 第 6 步：条数夹取 ----------------


def test_clamp_max() -> None:
    d = decide_k(
        [_c("a", 0.9), _c("b", 0.8), _c("c", 0.7)],
        **{**BASE, "max_k": 2},
    )
    assert d.selected == ["a", "b"]
    assert d.reason == "clamp_max"
    assert d.dropped_clamp == 1


def test_clamp_min_is_reported_but_does_not_pad() -> None:
    """候选不足时**不补低分凑数**，只归因 clamp_min。"""
    d = decide_k([_c("a", 0.9)], **{**BASE, "min_k": 5})
    assert d.selected == ["a"]
    assert d.reason == "clamp_min"


# ---------------- 强制保留 ----------------


def test_forced_survives_min_score() -> None:
    """pinned / unresolved 命中项不参与过滤。"""
    d = decide_k(
        [_c("pinned", 0.01, forced=True), _c("normal", 0.2)],
        **{**BASE, "min_score": 0.5},
    )
    assert "pinned" in d.selected
    assert "normal" not in d.selected


def test_forced_survives_drop_ratio_and_gap() -> None:
    d = decide_k(
        [_c("top", 1.0), _c("pinned", 0.01, forced=True)],
        **{**BASE, "drop_ratio": 0.9, "gap_ratio": 0.1},
    )
    assert "pinned" in d.selected


def test_forced_overflow_is_reported() -> None:
    """强制项**多于** max_k 时全留并标记 overflow，绝不静默截断。"""
    d = decide_k(
        [
            _c("p1", 0.9, forced=True),
            _c("p2", 0.8, forced=True),
            _c("p3", 0.7, forced=True),
            _c("n", 0.6),
        ],
        **{**BASE, "max_k": 2},
    )
    assert d.overflow is True
    assert d.reason == "overflow_forced"
    assert set(d.selected) == {"p1", "p2", "p3"}
    # 强制项占满后，普通项被挤出
    assert "n" not in d.selected


def test_forced_equal_to_max_k_is_not_overflow() -> None:
    """强制项刚好等于上限不算溢出——还装得下，只是普通项没位置。"""
    d = decide_k(
        [_c("p1", 0.9, forced=True), _c("p2", 0.8, forced=True), _c("n", 0.7)],
        **{**BASE, "max_k": 2},
    )
    assert d.overflow is False
    assert d.reason == "clamp_max"
    assert set(d.selected) == {"p1", "p2"}


def test_forced_comes_first_in_order() -> None:
    d = decide_k([_c("n", 0.99), _c("p", 0.1, forced=True)], **BASE)
    assert d.selected[0] == "p"


def test_forced_consumes_budget() -> None:
    d = decide_k(
        [_c("p", 0.9, tokens=800, forced=True), _c("n", 0.8, tokens=400)],
        **{**BASE, "budget_tokens": 1000},
    )
    assert d.selected == ["p"]


# ---------------- 参数校验 ----------------


def test_min_greater_than_max_rejected() -> None:
    with pytest.raises(ValueError, match="min_k"):
        decide_k([], **{**BASE, "min_k": 5, "max_k": 2})


def test_negative_budget_rejected() -> None:
    with pytest.raises(ValueError, match="budget_tokens"):
        decide_k([], **{**BASE, "budget_tokens": -1})


@pytest.mark.parametrize("field", ["drop_ratio", "gap_ratio"])
def test_ratio_out_of_range_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        decide_k([], **{**BASE, field: 1.5})


def test_trace_is_json_friendly() -> None:
    d = decide_k([_c("a", 0.5)], **BASE)
    trace = d.to_trace()
    assert trace["k"] == 1
    assert trace["reason"] == "all"
    assert trace["dropped"]["min_score"] == 0
