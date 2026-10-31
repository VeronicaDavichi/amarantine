"""B5 打标复核队列的测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory.services.label_review import LabelReviewQueue, queue_from_settings


@pytest.fixture
def queue(tmp_path) -> LabelReviewQueue:
    return LabelReviewQueue(tmp_path / "review.sqlite3")


def _push(queue: LabelReviewQueue, eid: str = "evt_1", **kw):
    params = {
        "namespace": "work",
        "event_id": eid,
        "summary": "张三决定项目A延期",
        "rules_types": ["work"],
        "llm_types": ["decision", "work"],
        "agreement": 0.5,
    }
    params.update(kw)
    return queue.push(**params)


# ---------------- 基本读写 ----------------


def test_push_then_list(queue) -> None:
    assert _push(queue) is True
    items = queue.list_pending("work")
    assert len(items) == 1
    it = items[0]
    assert it.event_id == "evt_1"
    assert it.rules_types == ["work"]
    assert it.llm_types == ["decision", "work"]
    assert it.agreement == pytest.approx(0.5)
    assert it.resolved_at is None


def test_namespace_isolation(queue) -> None:
    """A24 的另一半：不同 namespace 的队列不能串。"""
    _push(queue, "evt_1", namespace="work")
    _push(queue, "evt_1", namespace="life")
    assert len(queue.list_pending("work")) == 1
    assert len(queue.list_pending("life")) == 1
    assert queue.count_pending("work") == 1
    assert queue.count_pending("other") == 0


def test_push_is_idempotent_and_resets_resolution(queue) -> None:
    """同一张卡再次分歧 → 覆盖并重新变为未决。"""
    _push(queue)
    queue.resolve("work", "evt_1", resolution="accepted")
    assert queue.count_pending("work") == 0

    created = _push(queue, llm_types=["health"], agreement=0.0)
    assert created is False          # 已有记录，被覆盖
    assert queue.count_pending("work") == 1
    items = queue.list_pending("work")
    assert items[0].llm_types == ["health"]
    assert items[0].resolved_at is None


def test_resolve(queue) -> None:
    _push(queue)
    assert queue.resolve("work", "evt_1") is True
    assert queue.count_pending("work") == 0
    it = queue.get("work", "evt_1")
    assert it is not None
    assert it.resolved_at is not None
    assert it.resolution == "accepted"


def test_resolve_missing_returns_false(queue) -> None:
    assert queue.resolve("work", "evt_nope") is False


def test_resolve_twice_second_is_noop(queue) -> None:
    _push(queue)
    assert queue.resolve("work", "evt_1") is True
    assert queue.resolve("work", "evt_1") is False   # 已处理过


def test_resolve_does_not_delete(queue) -> None:
    """复核完不是删记录——留痕，方便回溯「当初为什么这么判」。"""
    _push(queue)
    queue.resolve("work", "evt_1", resolution="kept_rules")
    it = queue.get("work", "evt_1")
    assert it is not None
    assert it.resolution == "kept_rules"
    assert it.rules_types == ["work"]


# ---------------- 排序与条数 ----------------


def test_list_is_fifo(queue) -> None:
    clock = iter(
        [
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 1, 3, tzinfo=timezone.utc),
        ]
    )
    q = LabelReviewQueue(queue._path, clock=lambda: next(clock))
    _push(q, "evt_b")
    _push(q, "evt_a")
    _push(q, "evt_c")
    ids = [i.event_id for i in q.list_pending("work")]
    assert ids == ["evt_b", "evt_a", "evt_c"]


def test_limit(queue) -> None:
    for i in range(5):
        _push(queue, f"evt_{i}")
    assert len(queue.list_pending("work", limit=2)) == 2
    assert queue.count_pending("work") == 5


def test_get_missing(queue) -> None:
    assert queue.get("work", "evt_nope") is None


def test_empty_queue(queue) -> None:
    assert queue.list_pending("work") == []
    assert queue.count_pending("work") == 0


# ---------------- 持久化 ----------------


def test_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "review.sqlite3"
    q1 = LabelReviewQueue(path)
    _push(q1)
    q2 = LabelReviewQueue(path)
    assert q2.count_pending("work") == 1


def test_creates_parent_dir(tmp_path) -> None:
    nested = tmp_path / "a" / "b" / "review.sqlite3"
    LabelReviewQueue(nested)
    assert nested.exists()


def test_empty_types_lists_roundtrip(queue) -> None:
    _push(queue, rules_types=[], llm_types=[])
    it = queue.get("work", "evt_1")
    assert it is not None
    assert it.rules_types == []
    assert it.llm_types == []


def test_unicode_roundtrip(queue) -> None:
    _push(queue, summary="周会上讨论了明年预算")
    it = queue.get("work", "evt_1")
    assert it is not None
    assert it.summary == "周会上讨论了明年预算"


def test_to_dict(queue) -> None:
    _push(queue)
    d = queue.list_pending("work")[0].to_dict()
    assert d["event_id"] == "evt_1"
    assert d["rules_types"] == ["work"]
    assert d["llm_types"] == ["decision", "work"]
    assert d["agreement"] == 0.5
    assert d["resolved_at"] is None


def test_queue_from_settings(tmp_path) -> None:
    from memory.config import load_settings

    s = load_settings().model_copy(
        update={"DATABASE_URL": f"sqlite:///{(tmp_path / 'x.sqlite3').as_posix()}"}
    )
    q = queue_from_settings(s)
    _push(q)
    assert q.count_pending("work") == 1
