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

def test_real_resolver_confidence_is_binary() -> None:
    """当前 `time_resolver` 只会给 0.0 或 1.0。

    这条测试**不是**在保护这个行为 —— 而是在记录它。
    等有人给模糊表达加中间置信度时，这条会挂，提醒他回来看 015 的 Notes
    和 `TIME_CONF_MIN` 的闸门是否要一起调整。
    """
    from memory.core.time_resolver import resolve_from_query

    samples = ["今天", "上个月", "去年底", "本周", "完全不是时间", ""]
    seen = {resolve_from_query(s, today=date(2026, 11, 4)).confidence for s in samples}
    assert seen <= {0.0, 1.0}, f"置信度出现中间值 {seen} —— 015 的 Notes 需要更新"
