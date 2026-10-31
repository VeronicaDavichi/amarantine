"""B3 护栏单测。

重点覆盖 A31 / A33：
- A31：显式「记住」进 PROTECTED；`important` 与情绪永不升 PROTECTED
- A33：24h 第 21 次「记住」不再 pin，降级为 explicit_important 并记 anomaly
"""

from __future__ import annotations

import pytest

from memory.core.guards import (
    ANOMALY_DIRECTIVE_ABUSE,
    GuardError,
    apply_directive_guards,
    merge_importance,
    validate_remember_args,
)

DEFAULTS = dict(
    pinned_in_window=0,
    daily_max=20,
    explicit_important_floor=0.85,
    emotion_boost=0.15,
)


# ---------------- 参数校验 ----------------


def test_validate_accepts_normal_args() -> None:
    validate_remember_args(
        summary="张三决定项目A延期", pinned=False, important=False, emotion_intensity=0.0
    )


@pytest.mark.parametrize("bad", ["true", 1, None, 0])
def test_pinned_must_be_real_bool(bad: object) -> None:
    """LLM 常把 true 写成字符串；宽进会让护栏失效。"""
    with pytest.raises(GuardError, match="pinned 必须是布尔值"):
        validate_remember_args(
            summary="x", pinned=bad, important=False, emotion_intensity=0.0
        )


@pytest.mark.parametrize("bad", ["false", 0, None])
def test_important_must_be_real_bool(bad: object) -> None:
    with pytest.raises(GuardError, match="important 必须是布尔值"):
        validate_remember_args(
            summary="x", pinned=False, important=bad, emotion_intensity=0.0
        )


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2, "0.5", True])
def test_emotion_intensity_range(bad: object) -> None:
    with pytest.raises(GuardError, match="emotion_intensity"):
        validate_remember_args(
            summary="x", pinned=False, important=False, emotion_intensity=bad
        )


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_summary_must_be_non_empty_string(bad: object) -> None:
    with pytest.raises(GuardError, match="summary"):
        validate_remember_args(
            summary=bad, pinned=False, important=False, emotion_intensity=0.0
        )


# ---------------- 语言信号 ----------------


def test_explicit_remember_goes_protected() -> None:
    """A31：显式「记住」→ pinned + user_directive 来源。"""
    d = apply_directive_guards(pinned=True, important=False, emotion_intensity=0.0, **DEFAULTS)
    assert d.pinned is True
    assert d.pinned_source == "user_directive"
    assert d.user_directive == "remember"
    assert d.anomaly is None


def test_important_alone_never_protects() -> None:
    """重要宣告只给分数下限，不进 PROTECTED。"""
    d = apply_directive_guards(pinned=False, important=True, emotion_intensity=0.0, **DEFAULTS)
    assert d.pinned is False
    assert d.pinned_source is None
    assert d.importance_floor == 0.85
    assert d.user_directive == "important"


def test_emotion_never_protects() -> None:
    """情绪只加成，不构成保护（ADR 明确）。"""
    d = apply_directive_guards(pinned=False, important=False, emotion_intensity=0.9, **DEFAULTS)
    assert d.pinned is False
    assert d.pinned_source is None
    assert d.importance_floor is None
    assert d.importance_boost == pytest.approx(0.15 * 0.9)


def test_pinned_with_important_keeps_floor_none() -> None:
    """pin 已是最强保护，不再叠分下限。"""
    d = apply_directive_guards(pinned=True, important=True, emotion_intensity=0.0, **DEFAULTS)
    assert d.pinned is True
    assert d.importance_floor is None


# ---------------- 滥用护栏（A33） ----------------


def test_within_limit_still_pins() -> None:
    d = apply_directive_guards(
        pinned=True, important=False, emotion_intensity=0.0,
        **{**DEFAULTS, "pinned_in_window": 19},
    )
    assert d.pinned is True
    assert d.anomaly is None


def test_21st_remember_is_downgraded() -> None:
    """A33：24h 内第 21 次不再 pin，降级为 important 并记 anomaly。"""
    d = apply_directive_guards(
        pinned=True, important=False, emotion_intensity=0.0,
        **{**DEFAULTS, "pinned_in_window": 20},
    )
    assert d.pinned is False
    assert d.pinned_source is None
    assert d.user_directive is None
    assert d.important is True
    assert d.anomaly == ANOMALY_DIRECTIVE_ABUSE
    assert d.downgraded is True


def test_downgrade_keeps_emotion_boost() -> None:
    """降级只取消保护，不该顺手抹掉情绪加成。"""
    d = apply_directive_guards(
        pinned=True, important=False, emotion_intensity=0.5,
        **{**DEFAULTS, "pinned_in_window": 25},
    )
    assert d.pinned is False
    assert d.importance_boost == pytest.approx(0.075)


def test_zero_daily_max_is_rejected() -> None:
    with pytest.raises(ValueError, match="daily_max"):
        apply_directive_guards(
            pinned=True, important=False, emotion_intensity=0.0,
            **{**DEFAULTS, "daily_max": 0},
        )


# ---------------- 分数合并 ----------------


def test_merge_importance_takes_floor() -> None:
    assert merge_importance(base=0.5, floor=0.85, boost=0.0) == 0.85


def test_merge_importance_adds_boost() -> None:
    assert merge_importance(base=0.5, floor=None, boost=0.1) == pytest.approx(0.6)


def test_merge_importance_caps_at_one() -> None:
    assert merge_importance(base=0.95, floor=0.9, boost=0.3) == 1.0
