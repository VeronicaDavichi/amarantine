"""B2 时间线索解析单测。

规则层是事件切分的输入之一，必须可复现：同样 hint + today 永远同样区间。
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.core.time_resolver import FALLBACK_CONFIDENCE, HIGH_CONFIDENCE, resolve_date_hint

TODAY = date(2026, 10, 31)  # 周六


def test_none_falls_back_to_today() -> None:
    r = resolve_date_hint(None, today=TODAY)
    assert r.start == TODAY
    assert r.end is None
    assert r.confidence == FALLBACK_CONFIDENCE


def test_blank_falls_back() -> None:
    assert resolve_date_hint("   ", today=TODAY).start == TODAY


def test_iso_date() -> None:
    r = resolve_date_hint("2026-03-01", today=TODAY)
    assert (r.start, r.end, r.confidence) == (date(2026, 3, 1), None, HIGH_CONFIDENCE)


def test_invalid_iso_falls_back() -> None:
    r = resolve_date_hint("2026-02-31", today=TODAY)
    assert r.start == TODAY
    assert r.confidence == FALLBACK_CONFIDENCE


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("今天", date(2026, 10, 31)),
        ("昨天", date(2026, 10, 30)),
        ("前天", date(2026, 10, 29)),
    ],
)
def test_relative_days(hint: str, expected: date) -> None:
    r = resolve_date_hint(hint, today=TODAY)
    assert r.start == expected
    assert r.end is None


def test_last_month_is_full_range() -> None:
    """上个月 = 9/1 ~ 9/30，端点都要对。"""
    r = resolve_date_hint("上个月", today=TODAY)
    assert r.start == date(2026, 9, 1)
    assert r.end == date(2026, 9, 30)


def test_last_month_across_year_boundary() -> None:
    """1 月的「上个月」要跨年，不能算成 1 月。"""
    r = resolve_date_hint("上个月", today=date(2026, 1, 15))
    assert r.start == date(2025, 12, 1)
    assert r.end == date(2025, 12, 31)


def test_this_month_ends_today() -> None:
    r = resolve_date_hint("本月", today=TODAY)
    assert r.start == date(2026, 10, 1)
    assert r.end == TODAY


def test_last_year() -> None:
    r = resolve_date_hint("去年", today=TODAY)
    assert (r.start, r.end) == (date(2025, 1, 1), date(2025, 12, 31))


def test_last_year_end_is_q4() -> None:
    """「去年底」按 Q4 处理，不缩到单月。"""
    r = resolve_date_hint("去年底", today=TODAY)
    assert (r.start, r.end) == (date(2025, 10, 1), date(2025, 12, 31))


def test_chinese_year_month() -> None:
    r = resolve_date_hint("2026年3月", today=TODAY)
    assert (r.start, r.end) == (date(2026, 3, 1), date(2026, 3, 31))


def test_chinese_year() -> None:
    r = resolve_date_hint("2024年", today=TODAY)
    assert (r.start, r.end) == (date(2024, 1, 1), date(2024, 12, 31))


def test_this_week_starts_monday() -> None:
    """2026-10-31 是周六，本周应回到周一 10-26。"""
    r = resolve_date_hint("本周", today=TODAY)
    assert r.start == date(2026, 10, 26)
    assert r.end == TODAY


def test_unrecognized_text_falls_back() -> None:
    r = resolve_date_hint("某个时候吧大概", today=TODAY)
    assert r.start == TODAY
    assert r.confidence == FALLBACK_CONFIDENCE
