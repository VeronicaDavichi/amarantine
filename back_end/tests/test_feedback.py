"""E2 引用回路的测试：trace 存根 + feedback 幂等。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory.config import load_settings
from memory.services.feedback import FeedbackError, submit_feedback
from memory.services.recall_trace import (
    TRACE_PREFIX,
    RecallTraceStore,
    new_trace_id,
    trace_store_from_settings,
)

SETTINGS = load_settings()
NS = "work"


class FakeWeaviate:
    """只记下 `set_confirmed_by_use` 调用的假 store。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail = fail
        self.closed = False

    def set_confirmed_by_use(self, event_id: str, namespace: str, *, count: int) -> None:
        if self.fail:
            raise RuntimeError("weaviate 挂了")
        self.calls.append((event_id, namespace, count))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def store(tmp_path) -> RecallTraceStore:
    return RecallTraceStore(tmp_path / "trace.sqlite3")


def _seed(store: RecallTraceStore, ids: list[str], *, ns: str = NS) -> str:
    return store.record_trace(namespace=ns, query="测试查询", material_ids=ids)


def _fb(store, trace_id, ids, *, ns: str = NS, weaviate=None):
    return submit_feedback(
        namespace=ns,
        trace_id=trace_id,
        used_material_ids=ids,
        settings=SETTINGS,
        trace_store=store,
        store=weaviate or FakeWeaviate(),
    )


# ---------------- trace 存根 ----------------


def test_trace_id_format() -> None:
    tid = new_trace_id()
    assert tid.startswith(TRACE_PREFIX)
    assert len(tid) == len(TRACE_PREFIX) + 12
    assert all(c in "0123456789abcdef" for c in tid[len(TRACE_PREFIX):])


def test_trace_ids_are_unique() -> None:
    assert len({new_trace_id() for _ in range(50)}) == 50


def test_record_and_get(store) -> None:
    ids = ["evt_" + "a" * 16, "evt_" + "b" * 16]
    tid = _seed(store, ids)
    rec = store.get_trace(tid)
    assert rec is not None
    assert rec.namespace == NS
    assert rec.query == "测试查询"
    assert rec.material_ids == ids


def test_get_missing_trace(store) -> None:
    assert store.get_trace("tr_nope") is None


def test_record_trace_empty_ids(store) -> None:
    """空结果也要有 trace —— 让 LLM 能上报「一条都没用」。"""
    tid = _seed(store, [])
    rec = store.get_trace(tid)
    assert rec is not None
    assert rec.material_ids == []


def test_record_trace_uses_given_id(store) -> None:
    tid = store.record_trace(
        namespace=NS, query="q", material_ids=[], trace_id="tr_fixed"
    )
    assert tid == "tr_fixed"
    assert store.get_trace("tr_fixed") is not None


# ---------------- feedback 正常路径 ----------------


def test_feedback_accepts_returned_ids(store) -> None:
    ids = ["evt_" + "a" * 16, "evt_" + "b" * 16]
    tid = _seed(store, ids)
    wv = FakeWeaviate()
    out = _fb(store, tid, ["evt_" + "a" * 16], weaviate=wv)
    assert out.ok
    assert out.accepted == 1
    assert out.deduped == 0
    assert out.incremented == ["evt_" + "a" * 16]
    # 镜像到 Weaviate，写绝对值 1
    assert wv.calls == [("evt_" + "a" * 16, NS, 1)]


def test_feedback_tool_result_shape(store) -> None:
    tid = _seed(store, ["evt_" + "a" * 16])
    out = _fb(store, tid, ["evt_" + "a" * 16])
    assert out.to_tool_result() == {
        "accepted": 1,
        "deduped": 0,
        "confirmed_by_use_incremented": ["evt_" + "a" * 16],
    }


def test_feedback_partial_subset(store) -> None:
    """只报真正用了的那部分，没报的不计数。"""
    a, b, c = ("evt_" + x * 16 for x in "abc")
    tid = _seed(store, [a, b, c])
    out = _fb(store, tid, [a, c])
    assert out.accepted == 2
    assert set(out.incremented) == {a, c}
    assert store.count_feedback(NS, a) == 1
    assert store.count_feedback(NS, b) == 0


# ---------------- 幂等 ----------------


def test_feedback_is_idempotent(store) -> None:
    a = "evt_" + "a" * 16
    tid = _seed(store, [a])
    first = _fb(store, tid, [a])
    assert first.accepted == 1

    wv = FakeWeaviate()
    second = _fb(store, tid, [a], weaviate=wv)
    assert second.accepted == 0
    assert second.deduped == 1
    assert second.incremented == []
    # 重复上报不该再动 Weaviate
    assert wv.calls == []
    # 计数仍然是 1，没被累加
    assert store.count_feedback(NS, a) == 1


def test_duplicate_in_same_request_counted_once(store) -> None:
    a = "evt_" + "a" * 16
    tid = _seed(store, [a])
    out = _fb(store, tid, [a, a, a])
    assert out.accepted == 1
    assert out.deduped == 0       # 同一批内去重，不算 deduped
    assert store.count_feedback(NS, a) == 1


def test_absolute_value_not_increment(store) -> None:
    """镜像写的是绝对值：两次 feedback 后 Weaviate 收到 1、2，而不是 1、1。"""
    a = "evt_" + "a" * 16
    t1 = _seed(store, [a])
    t2 = _seed(store, [a])
    wv = FakeWeaviate()
    _fb(store, t1, [a], weaviate=wv)
    _fb(store, t2, [a], weaviate=wv)
    assert wv.calls == [(a, NS, 1), (a, NS, 2)]


# ---------------- 非法输入 ----------------


def test_feedback_unknown_trace(store) -> None:
    with pytest.raises(FeedbackError, match="不存在或已过期"):
        _fb(store, "tr_nope", ["evt_" + "a" * 16])


def test_feedback_id_not_in_trace(store) -> None:
    """ADR：只接受该 trace 实际返回过的 id，否则 isError。"""
    tid = _seed(store, ["evt_" + "a" * 16])
    with pytest.raises(FeedbackError, match="不在该 trace 的返回集里"):
        _fb(store, tid, ["evt_" + "b" * 16])


def test_feedback_mixed_valid_invalid_rejects_whole_request(store) -> None:
    """整单校验：一条非法就全拒，不做部分成功。"""
    a = "evt_" + "a" * 16
    tid = _seed(store, [a])
    with pytest.raises(FeedbackError):
        _fb(store, tid, [a, "evt_" + "z" * 16])
    # 合法的那个也**不该**被记账
    assert store.count_feedback(NS, a) == 0


def test_feedback_empty_trace_accepts_nothing(store) -> None:
    tid = _seed(store, [])
    with pytest.raises(FeedbackError):
        _fb(store, tid, ["evt_" + "a" * 16])


def test_feedback_cross_namespace_rejected(store) -> None:
    """跨域引用一律拒绝，不泄露别的 namespace 有没有这张卡。"""
    a = "evt_" + "a" * 16
    tid = _seed(store, [a], ns="other")
    with pytest.raises(FeedbackError, match="不属于当前记忆域"):
        _fb(store, tid, [a], ns=NS)


def test_feedback_blank_trace_id(store) -> None:
    with pytest.raises(FeedbackError, match="trace_id"):
        _fb(store, "  ", ["evt_" + "a" * 16])


def test_feedback_empty_ids(store) -> None:
    tid = _seed(store, ["evt_" + "a" * 16])
    with pytest.raises(FeedbackError, match="used_material_ids"):
        _fb(store, tid, [])


def test_feedback_non_string_id(store) -> None:
    tid = _seed(store, ["evt_" + "a" * 16])
    with pytest.raises(FeedbackError, match="非法项"):
        _fb(store, tid, [123])  # type: ignore[list-item]


# ---------------- 降级 ----------------


def test_weaviate_mirror_failure_does_not_lose_truth(tmp_path) -> None:
    """镜像失败只 warn，真值（SQLite）必须已记上。"""
    q = RecallTraceStore(tmp_path / "t.sqlite3")
    a = "evt_" + "a" * 16
    tid = _seed(q, [a])
    out = submit_feedback(
        namespace=NS,
        trace_id=tid,
        used_material_ids=[a],
        settings=SETTINGS,
        trace_store=q,
        store=FakeWeaviate(fail=True),
    )
    assert out.accepted == 1
    assert q.count_feedback(NS, a) == 1


def test_feedback_after_trace_pruned(store) -> None:
    """trace 存根过期清理后，feedback 自然报错而不是静默通过。"""
    a = "evt_" + "a" * 16
    tid = _seed(store, [a])
    assert store.prune_traces(keep_days=-1) >= 1   # 负数 = 全清
    assert store.get_trace(tid) is None
    with pytest.raises(FeedbackError, match="不存在或已过期"):
        _fb(store, tid, [a])


def test_prune_keeps_feedback(store) -> None:
    """清 trace 不动 feedback —— 后者是长期数据。"""
    a = "evt_" + "a" * 16
    tid = _seed(store, [a])
    _fb(store, tid, [a])
    store.prune_traces(keep_days=-1)
    assert store.count_feedback(NS, a) == 1


# ---------------- 其他 ----------------


def test_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "t.sqlite3"
    q1 = RecallTraceStore(path)
    a = "evt_" + "a" * 16
    tid = _seed(q1, [a])
    _fb(q1, tid, [a], weaviate=FakeWeaviate())
    q2 = RecallTraceStore(path)
    assert q2.get_trace(tid) is not None
    assert q2.count_feedback(NS, a) == 1


def test_trace_store_from_settings(tmp_path) -> None:
    s = SETTINGS.model_copy(
        update={"DATABASE_URL": f"sqlite:///{(tmp_path / 'x.sqlite3').as_posix()}"}
    )
    q = trace_store_from_settings(s)
    tid = q.record_trace(namespace=NS, query="q", material_ids=[])
    assert q.get_trace(tid) is not None


def test_count_feedback_namespace_isolated(store) -> None:
    a = "evt_" + "a" * 16
    t1 = _seed(store, [a], ns="work")
    t2 = _seed(store, [a], ns="life")
    _fb(store, t1, [a], ns="work")
    _fb(store, t2, [a], ns="life")
    assert store.count_feedback("work", a) == 1
    assert store.count_feedback("life", a) == 1
