"""C3 时效/重要性加权单测（ADR B1 公式）。

核心不变量：**基座加权不归零**。旧记忆会被衰减，但不会被彻底抹掉。
"""

from __future__ import annotations

import pytest

from memory.core.scoring import (
    DEFAULT_HALF_LIFE_DAYS,
    freshness,
    normalize_relevance,
    weighted_score,
)


def test_freshness_half_life() -> None:
    assert freshness(0) == pytest.approx(1.0)
    assert freshness(DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.5)
    # 两个半衰期 → 1/4
    assert freshness(DEFAULT_HALF_LIFE_DAYS * 2) == pytest.approx(0.25)


def test_future_dates_get_no_bonus() -> None:
    """未来事件按 Δ=0 处理，不给额外加成，防止未来卡压过当前卡。"""
    assert freshness(-100) == pytest.approx(1.0)


def test_freshness_monotonic_decreasing() -> None:
    values = [freshness(d) for d in (0, 30, 180, 365, 3650)]
    assert all(a > b for a, b in zip(values, values[1:]))


def test_old_memory_is_decayed_not_zeroed() -> None:
    """这是设计要点：极旧的卡仍保留 70% × 80% 的基座。"""
    score = weighted_score(1.0, delta_days=100_000, importance=0.0)
    assert score == pytest.approx(0.7 * 0.8, abs=1e-3)


def test_recent_and_important_scores_highest() -> None:
    best = weighted_score(1.0, delta_days=0, importance=1.0)
    worst = weighted_score(1.0, delta_days=1_000, importance=0.0)
    assert best == pytest.approx(1.0)
    assert best > worst


def test_time_factor_never_exceeds_one() -> None:
    for days in (0, 10, 100, 1000):
        assert weighted_score(1.0, delta_days=days, importance=1.0) <= 1.0


def test_importance_has_smaller_effect_than_recency() -> None:
    """importance 基座 0.8 比 time 基座 0.7 高，所以同等变化下重要性影响更小。"""
    gain_by_importance = weighted_score(1.0, delta_days=0, importance=1.0) - weighted_score(
        1.0, delta_days=0, importance=0.0
    )
    gain_by_recency = weighted_score(1.0, delta_days=0, importance=0.0) - weighted_score(
        1.0, delta_days=3650, importance=0.0
    )
    assert gain_by_importance < gain_by_recency


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_importance_out_of_range_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="importance"):
        weighted_score(1.0, delta_days=0, importance=bad)


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_base_weights_out_of_range_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="w_time_base"):
        weighted_score(1.0, delta_days=0, importance=0.5, w_time_base=bad)


def test_non_positive_half_life_rejected() -> None:
    with pytest.raises(ValueError, match="half_life_days"):
        freshness(10, half_life_days=0)


def test_normalize_scales_to_unit_max() -> None:
    assert normalize_relevance([2.0, 1.0, 0.5]) == pytest.approx([1.0, 0.5, 0.25])


def test_normalize_handles_all_zero() -> None:
    assert normalize_relevance([0.0, 0.0]) == [0.0, 0.0]


def test_normalize_empty() -> None:
    assert normalize_relevance([]) == []
