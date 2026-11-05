"""意图日志与 remember / 补跑 / 维护循环的整合。

守的核心约束：

    **图写失败不再静默。**
    `graph_ok` 这个字段原来全代码库没人读 —— 记忆进了 Weaviate
    （向量通道能召回），图关系却缺失（图通道永远找不到），
    调用方看到的是「成功」。这叫只进了一半。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from memory.config import load_settings
from memory.services.graph_outbox import OutboxResult
from memory.services.maintenance import MaintenanceWorker
from memory.services.pending_task import (
    KIND_GRAPH_REBUILD,
    KIND_GRAPH_WRITE,
    MAX_ATTEMPTS,
    PendingTaskStore,
    retry_pending,
)
from memory.services.remember import remember_event
from memory.services.weaviate_store import to_properties

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 2)
NS = "work"
SUMMARY = "张三决定项目A延期到六月"


@pytest.fixture
def store(tmp_path) -> PendingTaskStore:
    return PendingTaskStore(tmp_path / "intent.sqlite3")


# ---------------- 替身 ----------------

class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def ensure_collection(self) -> None:
        pass

    def get_by_event_id(self, namespace: str, event_id: str):
        return self.rows.get((namespace, event_id))

    def insert_event(self, card, vector, *, anomaly=None, vocab_version=None) -> str:
        uid = f"uuid-{card.event_id}"
        self.rows[(card.namespace, card.event_id)] = {
            "id": uid,
            **to_properties(card, anomaly=anomaly),
        }
        return uid

    def close(self) -> None:
        pass


class FakeCounter:
    def count_pinned(self, namespace: str, *, now) -> int:
        return 0

    def record_pinned(self, namespace: str, *, now) -> None:
        pass


def _embedder(text: str, settings) -> list[float]:
    return [0.01] * settings.EMBEDDING_DIM


def _ok_graph(card, *, settings, weaviate_id, context=None):
    return OutboxResult(ok=True)


def _bad_graph(card, *, settings, weaviate_id, context=None):
    return OutboxResult(ok=False, error="neo4j down")


def _write(intent: PendingTaskStore, *, graph_writer=_ok_graph, **overrides):
    """跑一次 remember。

    第一个参数是**意图日志**（不是 Weaviate 替身）—— 名字不能叫 `store`，
    否则跟 `overrides` 里给 `remember_event` 的 `store=` 撞名。
    """
    kwargs = dict(
        namespace=NS,
        summary=SUMMARY,
        settings=load_settings(),
        store=FakeStore(),
        counter=FakeCounter(),
        embedder=_embedder,
        graph_writer=graph_writer,
        intent_store=intent,
        now=NOW,
        today=TODAY,
    )
    kwargs.update(overrides)
    return remember_event(**kwargs)


# ---------------- 记账 ----------------

def test_success_leaves_no_row(store: PendingTaskStore) -> None:
    """写成功就销账，表里不该积灰。"""
    result = _write(store)
    assert result.graph_ok is True
    assert store.stats(NS)["total"] == 0


def test_failure_leaves_pending_row(store: PendingTaskStore) -> None:
    result = _write(store, graph_writer=_bad_graph)
    assert result.graph_ok is False
    assert result.graph_pending is True
    assert store.stats(NS)["pending"] == 1

    task = store.ready(NS)[0]
    assert task.kind == KIND_GRAPH_WRITE
    assert task.dedupe_key == result.event_id
    assert task.last_error == "neo4j down"


def test_payload_holds_full_card(store: PendingTaskStore) -> None:
    """payload 要能一比一还原 —— 否则补跑出来的是残缺的图。"""
    from memory.models.event_card import EventCard

    result = _write(store, graph_writer=_bad_graph)
    payload = store.ready(NS)[0].payload
    card = EventCard.model_validate(payload["card"])
    assert card.event_id == result.event_id
    assert card.summary == SUMMARY
    assert card.types, "types 必须存下来"
    assert card.retention
    assert payload["weaviate_id"] == result.weaviate_id


def test_idempotent_rewrite_is_also_accounted(store: PendingTaskStore) -> None:
    """重报同一条时图写失败，也要记账。

    幂等短路（事件已存在）那条分支原来**没有记账**：
    第一次图挂了，再提一次只会返回 `created=False` 而图始终是空的，
    永远修不好。两条写入路径必须用同一份记账逻辑。
    """
    fake = FakeStore()
    # 第一次：图成功
    first = _write(store, store=fake)
    assert first.created is True
    assert store.stats(NS)["total"] == 0

    # 第二次：同内容，图挂了 → 走幂等分支
    second = _write(store, store=fake, graph_writer=_bad_graph)
    assert second.created is False, "应该走幂等短路"
    assert second.graph_ok is False
    assert second.graph_pending is True, "幂等分支也必须记账"
    assert store.stats(NS)["pending"] == 1
    assert store.ready(NS, now=NOW + timedelta(minutes=5))[0].dedupe_key == first.event_id


def test_idempotent_rewrite_clean_when_graph_ok(store: PendingTaskStore) -> None:
    fake = FakeStore()
    _write(store, store=fake)
    second = _write(store, store=fake)
    assert second.created is False
    assert second.graph_ok is True
    assert second.graph_pending is False
    assert store.stats(NS)["total"] == 0


# ---------------- 不再静默 ----------------

def test_tool_result_surfaces_graph_failure(store: PendingTaskStore) -> None:
    """图没写成，调用方必须看得见。

    原来 `to_tool_result` 只回 event_id/created，`graph_ok=False`
    在返回里躺着但没人读 —— 等于静默降级。
    """
    result = _write(store, graph_writer=_bad_graph)
    payload = result.to_tool_result()
    assert payload["graph_pending"] is True
    assert payload["degraded"] == "graph_write_failed"


def test_tool_result_clean_on_success(store: PendingTaskStore) -> None:
    """成功时不要多嘴，别把正常路径的返回撑大。"""
    payload = _write(store).to_tool_result()
    assert "graph_pending" not in payload
    assert "degraded" not in payload


# ---------------- 补跑 ----------------

def test_retry_recovers(store: PendingTaskStore, monkeypatch) -> None:
    """图恢复后补跑，账销掉，写的是同一张卡。"""
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)
    seen: list[str] = []

    def _recover(card, *, settings, weaviate_id, context=None):
        seen.append(card.event_id)
        return OutboxResult(ok=True)

    monkeypatch.setattr(go, "write_event_node", _recover)
    report = retry_pending(
        NS, settings=load_settings(), store=store, now=NOW + timedelta(minutes=5)
    )

    assert report.attempted == 1
    assert report.recovered == 1
    assert store.stats(NS)["total"] == 0
    assert seen, "补跑必须真的调了写图"


def test_retry_replay_is_idempotent(store: PendingTaskStore, monkeypatch) -> None:
    """同一张卡重放两次，落到同一个 event_id —— 不会长出第二个节点。"""
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)
    ids: list[str] = []

    monkeypatch.setattr(
        go,
        "write_event_node",
        lambda card, **kw: (ids.append(card.event_id), OutboxResult(ok=True))[1],
    )
    retry_pending(
        NS, settings=load_settings(), store=store, now=NOW + timedelta(minutes=5)
    )
    retry_pending(
        NS, settings=load_settings(), store=store, now=NOW + timedelta(minutes=9)
    )
    # 第二次已经没有待办了
    assert len(ids) == 1


def test_retry_failure_backs_off(store: PendingTaskStore, monkeypatch) -> None:
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)
    monkeypatch.setattr(
        go, "write_event_node", lambda card, **kw: OutboxResult(ok=False, error="still down")
    )
    report = retry_pending(
        NS, settings=load_settings(), store=store, now=NOW + timedelta(minutes=5)
    )
    assert report.still_pending == 1
    task = store.get(store.ready(NS, now=NOW + timedelta(hours=2))[0].id)
    assert task.attempts == 2  # 原始失败 1 次 + 补跑失败 1 次


def test_retry_marks_dead_after_limit(store: PendingTaskStore, monkeypatch) -> None:
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)
    monkeypatch.setattr(
        go, "write_event_node", lambda card, **kw: OutboxResult(ok=False, error="down")
    )
    # 每次失败都会把 next_retry_at 往后推（指数退避），所以时间点要跟着走
    later = NOW
    for _ in range(MAX_ATTEMPTS + 1):
        later = later + timedelta(hours=2)
        retry_pending(NS, settings=load_settings(), store=store, now=later)
    assert store.stats(NS)["dead"] == 1
    assert store.stats(NS)["total"] == 1, "dead 的记录不能删"


def test_retry_survives_exception(store: PendingTaskStore, monkeypatch) -> None:
    """写图抛异常（不只是返回 ok=False）也要记成失败，不能把整批带崩。"""
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)

    def _boom(card, **kw):
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(go, "write_event_node", _boom)
    report = retry_pending(
        NS, settings=load_settings(), store=store, now=NOW + timedelta(minutes=5)
    )
    assert report.attempted == 1
    assert store.ready(NS, now=NOW + timedelta(hours=2))[0].last_error.startswith(
        "RuntimeError"
    )


# ---------------- 维护循环 ----------------

def test_tick_retries(store: PendingTaskStore, monkeypatch) -> None:
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)
    monkeypatch.setattr(go, "write_event_node", lambda card, **kw: OutboxResult(ok=True))

    worker = MaintenanceWorker(
        NS,
        load_settings(),
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_on_start=False,
    )
    report = worker.tick(now=NOW + timedelta(minutes=5))
    assert report.retry.recovered == 1


def test_tick_never_raises(store: PendingTaskStore, monkeypatch) -> None:
    """一个 tick 炸了循环不能死 —— 死了就没人补课了。"""
    import memory.services.graph_outbox as go

    _write(store, graph_writer=_bad_graph)

    def _explode(*a, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(go, "write_event_node", _explode)
    worker = MaintenanceWorker(
        NS,
        load_settings(),
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_on_start=False,
    )
    worker.tick(now=NOW)  # 不该抛


def test_reconcile_runs_on_first_tick_only_daily(store: PendingTaskStore, monkeypatch) -> None:
    """对账是 O(n)，不能每个 tick 都跑。"""
    calls: list[int] = []

    import memory.services.maintenance as m

    def _fake_reconcile(ns, **kw):
        calls.append(1)
        from memory.services.pending_task import ReconcileReport

        return ReconcileReport()

    monkeypatch.setattr(m, "reconcile_graph", _fake_reconcile)
    worker = MaintenanceWorker(
        NS, load_settings(), store=store, reconcile_every_s=86400, sweep_on_start=False
    )

    worker.tick(now=NOW)
    assert len(calls) == 1, "第一次 tick 要对账（补跨库窗口最好的时机）"
    worker.tick(now=NOW + timedelta(minutes=1))
    assert len(calls) == 1, "一分钟内不该再对账"
    worker.tick(now=NOW + timedelta(days=2))
    assert len(calls) == 2


# ---------------- 对账补图 ----------------

def test_rebuild_card_is_low_fidelity() -> None:
    """`graph_rebuild` 只能补 Weaviate 存得下的东西。

    persons / actions / alias_claims 在 `SCHEMA_PROPERTIES` 里根本没有，
    所以补出来的是 Event + Time + MENTIONS，INVOLVES / ACTION 补不回来。
    这条测试把这个限制写死，免得以后有人以为对账能完全修复。
    """
    from memory.services.pending_task import _rebuild_card

    card = _rebuild_card(
        KIND_GRAPH_REBUILD,
        {
            "namespace": NS,
            "event_id": "evt_0000000000000001",
            "summary": "张三决定项目A延期",
            "types": ["decision"],
            "retention": "normal",
            "ingested_at": NOW.isoformat(),
            "entity_names": ["张三", "项目A"],
        },
    )
    assert card.event_id == "evt_0000000000000001"
    # 实体补回来了，但 kind 只能是 topic（Weaviate 不存 kind）
    assert {e.name for e in card.entities} == {"张三", "项目A"}
    assert all(e.kind == "topic" for e in card.entities)
    # 人跟动作补不回来
    assert not card.actions
    assert not card.alias_claims
