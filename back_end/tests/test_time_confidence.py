"""015：`TIME_CONF_MIN` 真的决定「设不设时间过滤器」。

**这个 spec 有一个必须诚实说明的地方**：当前 `time_resolver` 的
`DateRange.confidence` 只有 `0.0` 和 `1.0` 两个值（`HIGH_CONFIDENCE` /
`FALLBACK_CONFIDENCE`），所以把 `TIME_CONF_MIN` 接上去**不改变默认行为**。

它的价值是：
1. 语义正确 —— 配置不再描述一个不存在的闸门
2. 未来安全 —— 一旦 `time_resolver` 开始给模糊表达中间置信度，闸门自动生效
3. `time_unresolved` 进 trace，可解释性有实际提升

所以下面的测试要**手动构造中间置信度**，否则测不到闸门本身。
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.core.time_resolver import DateRange
from tests.test_recall import FakeStore, _hit, _run


def _store():
    return FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")])


def _patch_date_range(monkeypatch, *, confidence: float, end: date | None, start: date):
    import memory.services.recall as mod

    monkeypatch.setattr(
        mod,
        "resolve_from_query",
        lambda query, today=None: DateRange(start, end, confidence, "手动构造"),
    )


def _settings(threshold: float = 0.5):
    from memory.config import load_settings

    s = load_settings()
    s.RERANK_ENABLED = False
    s.CHUNK_ENABLED = False
    s.TIME_CONF_MIN = threshold
    return s


# ---------------- D1 高置信 ----------------

def test_high_confidence_sets_date_filter(monkeypatch) -> None:
    _patch_date_range(
        monkeypatch, confidence=0.9, end=date(2026, 11, 30), start=date(2026, 11, 1)
    )
    result = _run(query="上个月的进展", settings=_settings(), store=_store())
    assert result.trace["time_unresolved"] is False
    assert result.trace["time_confidence"] == 0.9


# ---------------- D2 低置信 → 不设过滤器 ----------------

def test_low_confidence_skips_date_filter(monkeypatch) -> None:
    """0.3 < 0.5 → 退回全库，并标 unresolved。"""
    _patch_date_range(
        monkeypatch, confidence=0.3, end=date(2026, 11, 30), start=date(2026, 11, 1)
    )
    result = _run(query="最近怎么样", settings=_settings(), store=_store())
    assert result.trace["time_unresolved"] is True
    assert result.trace["time_confidence"] == 0.3


def test_low_confidence_still_returns_materials(monkeypatch) -> None:
    """关键：退回全库之后，材料必须**还在**。

    这条是「低置信宁可不设过滤器」这个取舍的全部意义 ——
    如果退回全库之后什么都搜不到，那不如按猜的窗口设。
    """
    _patch_date_range(
        monkeypatch, confidence=0.1, end=date(2026, 11, 30), start=date(2026, 11, 1)
    )
    result = _run(query="最近怎么样", settings=_settings(), store=_store())
    assert result.trace["time_unresolved"] is True
    assert result.materials, "低置信反而把材料搜空了 —— 取舍错了"


# ---------------- D3 end=None 不算 unresolved ----------------

def test_open_ended_range_is_not_unresolved(monkeypatch) -> None:
    """`end=None` 是「单日/持续中」的正常形态，不是解析失败。"""
    _patch_date_range(monkeypatch, confidence=0.2, end=None, start=date(2026, 11, 4))
    result = _run(query="昨天怎么样", settings=_settings(), store=_store())
    assert result.trace["time_unresolved"] is False, "把单日事件误标成解析失败"


# ---------------- D4 阈值 0 = 关闭闸门 ----------------

def test_zero_threshold_disables_the_gate(monkeypatch) -> None:
    """`TIME_CONF_MIN=0` 关闭闸门（沿用 A10/A25 的「0 = 关信号」约定）。"""
    _patch_date_range(
        monkeypatch, confidence=0.1, end=date(2026, 11, 30), start=date(2026, 11, 1)
    )
    result = _run(query="最近怎么样", settings=_settings(threshold=0), store=_store())
    assert result.trace["time_unresolved"] is False, "0 被误读成「全部不通过」"


def test_threshold_above_one_blocks_everything(monkeypatch) -> None:
    """阈值 > 1 时连 1.0 都过不去 —— 确认比较方向没写反。"""
    _patch_date_range(
        monkeypatch, confidence=1.0, end=date(2026, 11, 30), start=date(2026, 11, 1)
    )
    result = _run(query="上个月", settings=_settings(threshold=1.1), store=_store())
    assert result.trace["time_unresolved"] is True


# ---------------- 真实 time_resolver 的现状（钉住，便于未来改分级）----------------

def test_real_resolver_confidence_is_tiered() -> None:
    """018 之后是三档：1.0（精确/明确）/ 0.6（推算）/ 0.0（解析不出）。

    这条测试原本叫 `test_real_resolver_confidence_is_binary`，是 015 埋的钩子 ——
    「等有人加中间置信度时它会挂」。018 一改它就挂了，如期提醒了契约变更。
    现在它守着新的三档契约。
    """
    from memory.core.time_resolver import (
        APPROX_CONFIDENCE,
        FALLBACK_CONFIDENCE,
        HIGH_CONFIDENCE,
        resolve_from_query,
    )

    exact = ["2026-11-04", "2026年3月", "今天", "昨天", "本周", "上周", "上个月", "今年", "去年"]
    approx = ["上个月底", "上月底", "去年底", "上年底", "年底"]
    unresolved = ["完全不是时间", ""]

    for word in exact:
        got = resolve_from_query(word, today=date(2026, 11, 4)).confidence
        assert got == HIGH_CONFIDENCE, f"{word!r} 应该是精确档，实际 {got}"
    for word in approx:
        got = resolve_from_query(word, today=date(2026, 11, 4)).confidence
        assert got == APPROX_CONFIDENCE, f"{word!r} 应该是推算档，实际 {got}"
    for word in unresolved:
        got = resolve_from_query(word, today=date(2026, 11, 4)).confidence
        assert got == FALLBACK_CONFIDENCE, f"{word!r} 应该是解析不出，实际 {got}"


# ---------------- 018: 分级让配置第一次有真实区间 ----------------

def test_approx_tier_passes_the_default_threshold(monkeypatch) -> None:
    """`APPROX_CONFIDENCE(0.6) > TIME_CONF_MIN(0.5)` —— 默认行为不变。

    这是刻意的：分级不该偷偷改变现有行为。用户升级后感受不到差异。
    """
    _patch_date_range(
        monkeypatch, confidence=0.6, end=date(2026, 10, 31), start=date(2026, 10, 22)
    )
    result = _run(query="上个月底", settings=_settings(threshold=0.5), store=_store())
    assert result.trace["time_unresolved"] is False, "推算档在默认阈值下应该过线"


def test_raising_threshold_blocks_approx_tier(monkeypatch) -> None:
    """把闸门调到 0.7 → 推算档被挡掉。**配置第一次真的有用。**"""
    _patch_date_range(
        monkeypatch, confidence=0.6, end=date(2026, 10, 31), start=date(2026, 10, 22)
    )
    result = _run(query="上个月底", settings=_settings(threshold=0.7), store=_store())
    assert result.trace["time_unresolved"] is True


def test_raising_threshold_does_not_block_exact_tier(monkeypatch) -> None:
    """0.7 不该误伤精确档（1.0）—— 否则闸门变成了「全部不设过滤器」。"""
    _patch_date_range(
        monkeypatch, confidence=1.0, end=date(2026, 10, 31), start=date(2026, 10, 1)
    )
    result = _run(query="上个月", settings=_settings(threshold=0.7), store=_store())
    assert result.trace["time_unresolved"] is False
