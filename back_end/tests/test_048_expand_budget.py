"""spec 048 T5 补充：别名扩展的预算必须从总预算里预留。

踩过两次：

1. 用 `budget - elapsed` 当扩展预算 → 主召回常把预算用完，`remain` 为负，
   扩展从未执行（功能是死的）。
2. 改成完全独立的 800ms → 总耗时 = 主预算 + 800，突破 `RECALL_BUDGET_MS`。
"""

from __future__ import annotations

from memory.config import load_settings
from memory.services.recall import reserve_expand_budget


def _settings(**over):
    return load_settings().model_copy(update=over)


def test_no_expansions_reserves_nothing() -> None:
    """没有扩展词就不预留，否则主通道白少一半时间。"""
    assert reserve_expand_budget(1500, _settings(), has_expansions=False) == 0


def test_reserved_capped_at_half_budget() -> None:
    """预留不超过一半：扩展是增强，不能吃掉主通道。"""
    got = reserve_expand_budget(1000, _settings(ABBR_EXPAND_BUDGET_MS=800),
                                has_expansions=True)
    assert got == 500


def test_reserved_when_expand_budget_small() -> None:
    got = reserve_expand_budget(1500, _settings(ABBR_EXPAND_BUDGET_MS=300),
                                has_expansions=True)
    assert got == 300
    assert 1500 - got == 1200, "主通道应拿到剩余预算"


def test_zero_budget_reserves_nothing() -> None:
    assert reserve_expand_budget(0, _settings(), has_expansions=True) == 0


def test_negative_setting_clamped() -> None:
    """配置被写成负数时不能让主通道预算变大。"""
    got = reserve_expand_budget(1000, _settings(ABBR_EXPAND_BUDGET_MS=-50),
                                has_expansions=True)
    assert got == 0
