"""软删服务单测（A32）。

核心断言：忘掉之后**索引层查不到，但对象还在**（只打了 `deleted_at`）。
物理删除是 ADR 明令禁止的（N4）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory.config import load_settings
from memory.services.forget import ForgetError, forget_event
from memory.services.weaviate_store import WeaviateStoreError

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    """内存版 Weaviate：记录 mark_forgotten 调用，验证不发生删除。"""

    def __init__(self, *, exists: bool = True, already: bool = False, fail: bool = False):
        self.exists = exists
        self.already = already
        self.fail = fail
        self.marked: list[tuple[str, str, str]] = []
        self.deleted_calls = 0

    def get_by_event_id(self, namespace, event_id):
        if self.fail:
            raise WeaviateStoreError("down")
        if not self.exists:
            return None
        return {
            "namespace": namespace,
            "event_id": event_id,
            "summary": "x",
            "is_deleted": self.already,
            "deleted_at": "2026-10-01T00:00:00Z" if self.already else None,
        }

    def mark_forgotten(self, event_id, namespace, *, deleted_at):
        if self.fail:
            raise WeaviateStoreError("down")
        self.marked.append((event_id, namespace, deleted_at))

    def close(self):
        pass


def _run(store=None, **overrides):
    kwargs = dict(
        namespace="work",
        event_id="evt_a",
        reason="user_forget",
        settings=load_settings(),
        store=store or FakeStore(),
        now=NOW,
    )
    kwargs.update(overrides)
    return forget_event(**kwargs)


# ---------------- 正常软删 ----------------


def test_marks_deleted_at_without_deleting() -> None:
    store = FakeStore()
    result = _run(store=store)
    assert result.forgotten is True
    assert result.reason == "user_forget"
    assert len(store.marked) == 1
    eid, ns, at = store.marked[0]
    assert (eid, ns) == ("evt_a", "work")
    assert at == NOW.isoformat()
    # N4：绝不能调删除
    assert store.deleted_calls == 0


def test_outdated_reason_accepted() -> None:
    result = _run(reason="outdated")
    assert result.reason == "outdated"


def test_already_forgotten_is_idempotent() -> None:
    """重复忘同一张卡不该报错，也不该重复写。"""
    store = FakeStore(already=True)
    result = _run(store=store)
    assert result.forgotten is True
    assert store.marked == []


# ---------------- 失败路径 ----------------


def test_missing_event_raises() -> None:
    with pytest.raises(ForgetError, match="找不到"):
        _run(store=FakeStore(exists=False))


def test_store_down_raises() -> None:
    with pytest.raises(ForgetError, match="不可用"):
        _run(store=FakeStore(fail=True))


def test_bad_reason_rejected() -> None:
    with pytest.raises(ValueError, match="reason"):
        _run(reason="rm -rf")


def test_blank_event_id_rejected() -> None:
    with pytest.raises(ValueError, match="event_id"):
        _run(event_id="  ")


# ---------------- 图通道降级 ----------------


def test_graph_failure_does_not_fail_forget() -> None:
    """图可丢弃重建（D2），图标记失败不能导致遗忘失败。"""

    def broken_graph(ns, eid, reason):
        raise RuntimeError("neo4j down")

    result = _run(graph_writer=broken_graph)
    assert result.forgotten is True
    assert result.graph_ok is False


def test_graph_writer_called_on_success() -> None:
    calls = []

    def writer(ns, eid, reason):
        calls.append((ns, eid, reason))

    _run(graph_writer=writer)
    assert calls == [("work", "evt_a", "user_forget")]


def test_tool_result_shape() -> None:
    result = _run()
    assert result.to_tool_result() == {
        "event_id": "evt_a",
        "forgotten": True,
        "reason": "user_forget",
    }
