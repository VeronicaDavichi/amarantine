"""B2 时间线索解析（规则层）。

ADR C.B9 的 `time_resolver` 是「规则 + LLM 合并」。P0 只实现**规则层**：

- 能解析的：今天/昨天/前天、本周/上周、本月/上个月、今年/去年、去年底、ISO 日期
- 解析不了的：返回 ``(today, None)`` 并把置信度标低，由调用方决定是否让 LLM 补

为什么要规则先上：规则层可测、可复现、零 token 成本；
「上个月」这类表达占真实输入的绝大多数，LLM 兜底只处理长尾。

`date_end` 语义与 EventCard 一致：单日事件为 ``None``，区间事件为区间末日。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

#: 解析置信度低于该值时，调用方可考虑交给 LLM 复核（C.B9 / TIME_CONF_MIN）
HIGH_CONFIDENCE = 1.0
FALLBACK_CONFIDENCE = 0.0

_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_CN_YEAR_MONTH = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月$")
_CN_YEAR = re.compile(r"^(\d{4})\s*年$")


@dataclass(frozen=True)
class DateRange:
    """解析结果。``end=None`` 表示单日或持续中。"""

    start: date
    end: date | None
    confidence: float
    matched: str | None = None


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last)


def _shift_month(d: date, months: int) -> date:
    """按月平移，日号超出目标月长度时夹到月末。"""
    month_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last))


def resolve_date_hint(hint: str | None, *, today: date) -> DateRange:
    """把自然语言时间线索解析成日期区间。

    Args:
        hint: LLM 给出的时间线索，可为空。
        today: 「今天」的基准日（由调用方按 TIMEZONE 算好传入）。

    Returns:
        DateRange；无法识别时返回 ``(today, None, FALLBACK_CONFIDENCE)``。
    """
    if hint is None:
        return DateRange(today, None, FALLBACK_CONFIDENCE)
    text = hint.strip()
    if not text:
        return DateRange(today, None, FALLBACK_CONFIDENCE)

    # ---------- ISO / 数字日期 ----------
    if m := _ISO_DATE.match(text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return DateRange(today, None, FALLBACK_CONFIDENCE)
        return DateRange(d, None, HIGH_CONFIDENCE, text)

    if m := _CN_YEAR_MONTH.match(text):
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            return DateRange(today, None, FALLBACK_CONFIDENCE)
        first = date(year, month, 1)
        return DateRange(first, _month_end(first), HIGH_CONFIDENCE, text)

    if m := _CN_YEAR.match(text):
        year = int(m.group(1))
        return DateRange(date(year, 1, 1), date(year, 12, 31), HIGH_CONFIDENCE, text)

    # ---------- 相对日期 ----------
    if text == "今天":
        return DateRange(today, None, HIGH_CONFIDENCE, text)
    if text == "昨天":
        d = today - timedelta(days=1)
        return DateRange(d, None, HIGH_CONFIDENCE, text)
    if text == "前天":
        d = today - timedelta(days=2)
        return DateRange(d, None, HIGH_CONFIDENCE, text)

    # ---------- 周 ----------
    if text in ("本周", "这周"):
        start = today - timedelta(days=today.weekday())
        return DateRange(start, today, HIGH_CONFIDENCE, text)
    if text in ("上周", "上个星期"):
        start = today - timedelta(days=today.weekday() + 7)
        return DateRange(start, start + timedelta(days=6), HIGH_CONFIDENCE, text)

    # ---------- 月 ----------
    if text in ("本月", "这个月"):
        return DateRange(_month_start(today), today, HIGH_CONFIDENCE, text)
    if text in ("上个月", "上月"):
        last_month = _shift_month(today, -1)
        return DateRange(
            _month_start(last_month), _month_end(last_month), HIGH_CONFIDENCE, text
        )
    if text in ("上个月底", "上月底", "月末"):
        last_month = _shift_month(today, -1)
        end = _month_end(last_month)
        return DateRange(end - timedelta(days=9), end, HIGH_CONFIDENCE, text)

    # ---------- 年 ----------
    if text in ("今年", "本年"):
        return DateRange(date(today.year, 1, 1), today, HIGH_CONFIDENCE, text)
    if text in ("去年", "上一年"):
        y = today.year - 1
        return DateRange(date(y, 1, 1), date(y, 12, 31), HIGH_CONFIDENCE, text)
    if text in ("去年底", "上年底", "年底"):
        y = today.year - 1
        return DateRange(date(y, 10, 1), date(y, 12, 31), HIGH_CONFIDENCE, text)

    # ---------- 兜底 ----------
    return DateRange(today, None, FALLBACK_CONFIDENCE)


#: 可识别的时间词，按长度降序匹配（长词优先，避免「上个月底」被「上个月」截走）
_TIME_WORDS: tuple[str, ...] = tuple(
    sorted(
        {
            "上个月底", "上月底", "上个月", "上月", "这个月", "本月", "月末",
            "上个星期", "本周", "这周", "上周",
            "去年底", "上年底", "去年", "上一年", "今年", "本年",
            "今天", "昨天", "前天", "年底",
        },
        key=len,
        reverse=True,
    )
)

_ISO_IN_TEXT = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_CN_YM_IN_TEXT = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月")


def find_time_hint(text: str) -> str | None:
    """在自由文本（如用户 query）里找第一个可识别的时间词。

    召回时 query 往往是整句（「上个月项目A怎么样了」），
    需要先把时间片段摘出来再交给 `resolve_date_hint`。

    Returns:
        命中的时间词；无命中返回 None。
    """
    if not text:
        return None
    if m := _ISO_IN_TEXT.search(text):
        return m.group(0)
    if m := _CN_YM_IN_TEXT.search(text):
        return m.group(0)
    for word in _TIME_WORDS:
        if word in text:
            return word
    return None


def resolve_from_query(text: str, *, today: date) -> DateRange:
    """从整句 query 里解析时间区间（召回入口用）。"""
    hint = find_time_hint(text)
    if hint is None:
        return DateRange(today, None, FALLBACK_CONFIDENCE)
    return resolve_date_hint(hint, today=today)
