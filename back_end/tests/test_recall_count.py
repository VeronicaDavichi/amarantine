"""召回计数回写单测（A13 / A19）。

关键口径：只有「进了返回集 **且** 分数达标」才 +1。
分数不达标却被返回的条目，绝不能因此获得留存豁免。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory.services.recall_count import RecallCounter

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _counter(tmp_path: Path, **kwargs) -> RecallCounter:
    return RecallCounter(tmp_path / "test.db", **kwargs)


def test_records_when_score_passes(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    assert c.record("work", "evt_a", score=0.8, min_score=0.5, now=NOW) is True
    c.flush()
    assert c.stats("work", "evt_a") == (1, NOW.isoformat())


def test_skips_when_score_below_threshold(tmp_path: Path) -> None:
    """A19：低分命中不计数。"""
    c = _counter(tmp_path)
    assert c.record("work", "evt_a", score=0.1, min_score=0.5, now=NOW) is False
    c.flush()
    assert c.stats("work", "evt_a") == (0, None)


def test_repeated_recalls_accumulate(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    for _ in range(3):
        c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c.flush()
    assert c.stats("work", "evt_a")[0] == 3


def test_batch_auto_flush_at_threshold(tmp_path: Path) -> None:
    c = _counter(tmp_path, flush_every=3)
    for i in range(3):
        c.record("work", f"evt_{i}", score=0.9, min_score=0.5, now=NOW)
    # 达到阈值应已自动落库
    assert c.pending_count() == 0
    assert c.stats("work", "evt_0")[0] == 1


def test_batch_below_threshold_stays_pending(tmp_path: Path) -> None:
    c = _counter(tmp_path, flush_every=10, flush_interval_s=9999)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    assert c.pending_count() == 1
    c.flush()
    assert c.pending_count() == 0


def test_namespace_isolation(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c.record("personal", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c.flush()
    assert c.stats("work", "evt_a")[0] == 1
    assert c.stats("personal", "evt_a")[0] == 1


def test_record_many_counts_only_passing(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    counted = c.record_many(
        "work",
        [("evt_a", 0.9), ("evt_b", 0.2), ("evt_c", 0.6)],
        min_score=0.5,
        now=NOW,
    )
    c.flush()
    assert counted == 2
    assert c.stats("work", "evt_b") == (0, None)


def test_flush_empty_is_noop(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    assert c.flush() == 0


def test_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    c1 = RecallCounter(db)
    c1.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c1.flush()

    c2 = RecallCounter(db)
    assert c2.stats("work", "evt_a")[0] == 1


def test_last_recalled_at_updates(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c.flush()
    later = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=later)
    c.flush()
    count, at = c.stats("work", "evt_a")
    assert count == 2
    assert at == later.isoformat()


def test_clear(tmp_path: Path) -> None:
    c = _counter(tmp_path)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    c.flush()
    c.clear()
    assert c.stats("work", "evt_a") == (0, None)


def test_interval_based_flush(tmp_path: Path) -> None:
    """时间到了也要 flush（不是只看条数）。"""
    fake = [0.0]

    def clock() -> float:
        return fake[0]

    c = _counter(tmp_path, flush_every=100, flush_interval_s=10.0, clock=clock)
    c.record("work", "evt_a", score=0.9, min_score=0.5, now=NOW)
    assert c.pending_count() == 1
    fake[0] = 11.0  # 超过 10s
    c.record("work", "evt_b", score=0.9, min_score=0.5, now=NOW)
    assert c.pending_count() == 0
