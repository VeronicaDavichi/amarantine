"""spec 040：时间来源分层（stated / relative / msg_ts / imported）。

用户定的规则：
- 不知道时间 → 用**导入时间**，但必须标出来（`time_source=imported`）
- 消息时间 ≠ 事件时间（10-30 说「昨天」→ 事件是 10-29）
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.services import importer as IM


def test_relative_hint_extracted() -> None:
    assert IM.relative_time_hint("昨天开的会定下来了") == "昨天"
    assert IM.relative_time_hint("上周五发过一版") == "上周五"
    assert IM.relative_time_hint("前端页面改好了") is None


def test_relative_hint_prefers_single_token() -> None:
    """只回第一个相对时间词 —— 交给服务端换算，不做二次解释。"""
    assert IM.relative_time_hint("前天说的，昨天又改了一版") == "前天"


def test_msg_time_carries_full_timestamp() -> None:
    """消息时间要带时分秒 —— 事件时间可能不是消息当天。"""
    turns = [{"text": "任泽安：昨天开的会定下来了", "ts": "2026-10-30 14:50:00"}]
    times = IM.assign_event_msg_times(["任泽安说昨天开的会定下来了"], turns)
    assert times == ["2026-10-30 14:50:00"]


def test_msg_time_none_when_unmatched() -> None:
    turns = [{"text": "任泽安：昨天开的会", "ts": "2026-10-30 14:50:00"}]
    assert IM.assign_event_msg_times(["完全无关的一句话"], turns) == [None]


def test_relative_anchored_at_msg_date_not_today() -> None:
    """核心：同样说「昨天」，10-30 的消息换算出 10-29，10-31 的换算出 10-30。"""
    from memory.core.time_resolver import resolve_date_hint

    assert resolve_date_hint("昨天", today=date(2026, 10, 30)).start == date(2026, 10, 29)
    assert resolve_date_hint("昨天", today=date(2026, 10, 31)).start == date(2026, 10, 30)


@pytest.mark.parametrize(
    "hint,ambiguous",
    [
        ("昨天", "relative"),
        ("上周五", "relative"),
        ("2026-10-28", "stated"),
        ("10月28日", "stated"),
    ],
)
def test_time_source_classification(hint: str, ambiguous: str) -> None:
    """相对词 → relative（依赖消息时间换算）；明确日期 → stated。"""
    assert IM.time_source_for(hint, None) == ambiguous


def test_time_source_falls_back_to_msg_or_import() -> None:
    assert IM.time_source_for(None, "2026-10-30T14:50:00+08:00") == "msg_ts"
    assert IM.time_source_for(None, None) == "imported"


def test_parse_ts_accepts_iso_z() -> None:
    """QQ 语料的时间是 `2026-04-25T11:12:05Z` —— 不认 Z 会全部回落到导入日。

    实测后果：1963/1965 张卡的 `date_start` 都是导入日。
    """
    from memory.services.raw_quote import _parse_ts

    parsed = _parse_ts("2026-04-25T11:12:05Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 4 and parsed.day == 25
    assert parsed.tzinfo is not None


def test_parse_ts_accepts_offset_and_space_forms() -> None:
    from memory.services.raw_quote import _parse_ts

    assert _parse_ts("2026-04-25T11:12:05+08:00").hour == 11
    assert _parse_ts("2026-04-25 11:12:05").hour == 11
    assert _parse_ts("2026-04-25").day == 25
    assert _parse_ts("") is None
