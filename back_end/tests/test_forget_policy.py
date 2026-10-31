"""D7 遗忘三桶分流单测。

三条必须钉死的性质：
1. 硬信号绝对保护（pinned / unresolved / permanent retention）
2. 宽限期优先于淘汰（A11：新卡不进删除桶）
3. importance 只进 NEED_REVIEW，**绝不**自动豁免为 PROTECTED
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.core.forget_policy import (
    BUCKET_NEED_REVIEW,
    BUCKET_PROTECTED,
    BUCKET_SAFE_TO_DELETE,
    ForgetCandidate,
    classify,
    sweep,
)

TODAY = date(2026, 10, 31)
BASE = dict(
    today=TODAY,
    idle_days=180,
    min_recalls=1,
    grace_days=7,
    protect_importance=0.8,
)


def _c(**kwargs):
    defaults = dict(
        event_id="evt_x",
        importance=0.3,
        recall_count=0,
        created_at=date(2026, 1, 1),
        last_recalled_at=None,
        retention="normal",
        pinned=False,
        unresolved=[],
    )
    defaults.update(kwargs)
    return ForgetCandidate(**defaults)


# ---------------- 硬信号保护 ----------------


def test_pinned_is_protected() -> None:
    v = classify(_c(pinned=True, created_at=date(2020, 1, 1)), **BASE)
    assert v.bucket == BUCKET_PROTECTED
    assert v.reason == "pinned"
    assert not v.deletable


def test_unresolved_is_protected() -> None:
    v = classify(_c(unresolved=["等法务确认"], created_at=date(2020, 1, 1)), **BASE)
    assert v.bucket == BUCKET_PROTECTED
    assert v.reason == "unresolved"


@pytest.mark.parametrize("retention", ["protected", "permanent"])
def test_permanent_retention_is_protected(retention: str) -> None:
    v = classify(_c(retention=retention, created_at=date(2020, 1, 1)), **BASE)
    assert v.bucket == BUCKET_PROTECTED
    assert retention in v.reason


# ---------------- 宽限期（A11） ----------------


def test_new_card_never_enters_delete_bucket() -> None:
    """刚写入、还没被召回的卡，即使 idle 已经很大也不能被删。"""
    v = classify(_c(created_at=TODAY, recall_count=0), **BASE)
    assert v.bucket == BUCKET_PROTECTED
    assert v.reason == "grace_period"


def test_grace_expires_after_threshold() -> None:
    old = date(2026, 1, 1)
    v = classify(_c(created_at=old, recall_count=0), **BASE)
    assert v.bucket == BUCKET_SAFE_TO_DELETE


def test_recalled_card_in_grace_period_is_not_grace_protected() -> None:
    """宽限期只保护「新且没召回过」的卡；召回过说明活跃，走别的分支。"""
    v = classify(
        _c(created_at=TODAY, recall_count=3, last_recalled_at=TODAY), **BASE
    )
    assert v.reason != "grace_period"


# ---------------- 淘汰条件：四个全满足 ----------------


def test_all_forget_conditions_met() -> None:
    v = classify(
        _c(created_at=date(2025, 1, 1), recall_count=0, importance=0.1), **BASE
    )
    assert v.bucket == BUCKET_SAFE_TO_DELETE
    assert v.reason == "idle_and_unrecalled"


def test_recently_recalled_is_not_deletable() -> None:
    """recall_count 达标就不删——被用过的记忆有留存价值。"""
    v = classify(
        _c(
            created_at=date(2025, 1, 1),
            recall_count=5,
            last_recalled_at=date(2026, 10, 20),
        ),
        **BASE,
    )
    assert v.bucket != BUCKET_SAFE_TO_DELETE


def test_recent_recall_resets_idle() -> None:
    """last_recalled_at 比 created_at 新时，idle 从最后一次召回算。"""
    v = classify(
        _c(created_at=date(2024, 1, 1), last_recalled_at=date(2026, 10, 1)),
        **BASE,
    )
    assert v.idle_days == 30
    assert v.bucket != BUCKET_SAFE_TO_DELETE


# ---------------- importance 灰区 ----------------


def test_high_importance_goes_to_review_not_delete() -> None:
    v = classify(
        _c(created_at=date(2025, 1, 1), recall_count=0, importance=0.9), **BASE
    )
    assert v.bucket == BUCKET_NEED_REVIEW
    assert v.reason == "high_importance"


def test_high_importance_does_not_auto_protect() -> None:
    """D7 的立场：importance 不能一票否决，只能进复核队列。"""
    v = classify(_c(importance=1.0, created_at=date(2020, 1, 1)), **BASE)
    assert v.bucket == BUCKET_NEED_REVIEW
    assert v.bucket != BUCKET_PROTECTED


# ---------------- 兜底 ----------------


def test_active_card_is_protected() -> None:
    v = classify(
        _c(created_at=date(2026, 6, 1), last_recalled_at=date(2026, 10, 1)), **BASE
    )
    assert v.bucket == BUCKET_PROTECTED
    assert v.reason == "active"


# ---------------- sweep ----------------


def test_sweep_counts_all_buckets() -> None:
    report = sweep(
        [
            _c(event_id="p", pinned=True),
            _c(event_id="r", importance=0.95, created_at=date(2026, 6, 1)),
            _c(event_id="d", created_at=date(2025, 1, 1), importance=0.1),
        ],
        **BASE,
    )
    assert report.buckets[BUCKET_PROTECTED] == 1
    assert report.buckets[BUCKET_NEED_REVIEW] == 1
    assert report.buckets[BUCKET_SAFE_TO_DELETE] == 1
    assert len(report.verdicts) == 3
    assert report.dry_run is True


def test_sweep_refuses_non_dry_run() -> None:
    """A12：误删率闸门未过，P0 不允许真删。"""
    with pytest.raises(ValueError, match="dry_run"):
        sweep([], **BASE, dry_run=False)


def test_sweep_empty() -> None:
    report = sweep([], **BASE)
    assert report.buckets == {
        BUCKET_PROTECTED: 0,
        BUCKET_NEED_REVIEW: 0,
        BUCKET_SAFE_TO_DELETE: 0,
    }


def test_by_bucket_filter() -> None:
    report = sweep(
        [_c(event_id="p", pinned=True), _c(event_id="d", created_at=date(2025, 1, 1), importance=0.1)],
        **BASE,
    )
    assert [v.event_id for v in report.by_bucket(BUCKET_PROTECTED)] == ["p"]


# ---------------- 参数校验 ----------------


@pytest.mark.parametrize("field", ["idle_days", "min_recalls", "grace_days"])
def test_negative_params_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        classify(_c(), **{**BASE, field: -1})


def test_protect_importance_out_of_range() -> None:
    with pytest.raises(ValueError, match="protect_importance"):
        classify(_c(), **{**BASE, "protect_importance": 1.5})
