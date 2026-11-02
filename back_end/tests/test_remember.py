"""B1 `memory_remember` 编排单测。

外部依赖全部用替身：Weaviate / embedding / Neo4j / 计数器都不联网。
重点验证编排顺序与几条红线：

- A7 幂等：同内容重复写 → created=False，库里只一条
- A33 护栏：超限降级，且降级信息透传给调用方
- D2 图可丢弃：图写失败不影响主链路
- 参数非法 → GuardError（工具层转 isError）
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.config import load_settings
from memory.core.guards import GuardError
from memory.services.embedding import EmbeddingError
from memory.services.graph_outbox import OutboxResult
from memory.services.remember import remember_event
from memory.services.weaviate_store import MixedModelError, to_properties

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 31)


class FakeStore:
    """内存版 Weaviate，行为对齐真实 Store 的幂等语义。"""

    def __init__(self, *, fail_insert: bool = False) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.fail_insert = fail_insert
        self.insert_calls = 0
        self.ensure_calls = 0

    def ensure_collection(self) -> None:
        self.ensure_calls += 1

    def get_by_event_id(self, namespace: str, event_id: str) -> dict | None:
        return self.rows.get((namespace, event_id))

    def insert_event(self, card, vector, *, anomaly=None) -> str:
        self.insert_calls += 1
        if self.fail_insert:
            raise MixedModelError("模拟写入失败")
        uid = f"uuid-{card.event_id}"
        self.rows[(card.namespace, card.event_id)] = {
            "id": uid,
            **to_properties(card, anomaly=anomaly),
        }
        return uid

    def close(self) -> None:  # pragma: no cover - 仅保证接口齐全
        pass


class FakeCounter:
    def __init__(self, initial: int = 0) -> None:
        self.count = initial
        self.recorded = 0

    def count_pinned(self, namespace: str, *, now) -> int:
        return self.count

    def record_pinned(self, namespace: str, *, now) -> None:
        self.recorded += 1
        self.count += 1


def _embedder(text: str, settings) -> list[float]:
    return [0.01] * settings.EMBEDDING_DIM


def _ok_graph(card, *, settings, weaviate_id, context=None):
    return OutboxResult(ok=True)


def _bad_graph(card, *, settings, weaviate_id, context=None):
    return OutboxResult(ok=False, error="neo4j down")


def _run(**overrides):
    settings = load_settings()
    kwargs = dict(
        namespace="work",
        summary="张三决定项目A延期到六月",
        date_hint="上个月",
        settings=settings,
        store=FakeStore(),
        counter=FakeCounter(),
        embedder=_embedder,
        graph_writer=_ok_graph,
        now=NOW,
        today=TODAY,
    )
    kwargs.update(overrides)
    return remember_event(**kwargs)


# ---------------- 正常写入 ----------------


def test_first_write_creates_card() -> None:
    result = _run()
    assert result.created is True
    assert result.event_id.startswith("evt_")
    assert result.graph_ok is True
    assert result.anomaly is None


def test_card_lands_with_expected_fields() -> None:
    store = FakeStore()
    _run(store=store)
    assert len(store.rows) == 1
    (_, props) = next(iter(store.rows.items()))
    assert props["namespace"] == "work"
    # Weaviate 的 date 属性要求 RFC3339，所以纯日期会补成当天零点 UTC
    assert props["date_start"] == "2026-09-01T00:00:00Z"
    assert props["date_end"] == "2026-09-30T00:00:00Z"
    assert "decision" in props["types"]


# ---------------- A7 幂等 ----------------


def test_second_write_is_idempotent() -> None:
    store = FakeStore()
    first = _run(store=store)
    second = _run(store=store)
    assert first.event_id == second.event_id
    assert first.created is True
    assert second.created is False
    assert len(store.rows) == 1
    # 重复写入不应再调 insert
    assert store.insert_calls == 1


def test_idempotent_skip_does_not_consume_embedding() -> None:
    """幂等短路必须发生在 embed 之前，否则重复写会白烧算力。"""
    calls = {"n": 0}

    def counting_embedder(text: str, settings) -> list[float]:
        calls["n"] += 1
        return [0.02] * settings.EMBEDDING_DIM

    store = FakeStore()
    _run(store=store, embedder=counting_embedder)
    _run(store=store, embedder=counting_embedder)
    assert calls["n"] == 1


# ---------------- A33 护栏 ----------------


def test_pinned_is_recorded_after_success() -> None:
    counter = FakeCounter()
    result = _run(pinned=True, counter=counter)
    assert result.created is True
    assert counter.recorded == 1


def test_over_limit_is_downgraded() -> None:
    counter = FakeCounter(initial=20)
    result = _run(pinned=True, counter=counter)
    assert result.anomaly == "directive_abuse"
    # 降级后不再消耗配额
    assert counter.recorded == 0


def test_downgraded_card_is_important_not_protected() -> None:
    store = FakeStore()
    _run(pinned=True, counter=FakeCounter(initial=20), store=store)
    (_, props) = next(iter(store.rows.items()))
    assert props["pinned"] is False
    assert bool(props["importance"]) is True
    assert props["anomaly"] == "directive_abuse"
    assert "pinned_source" not in props


def test_write_failure_does_not_consume_quota() -> None:
    """写入失败还记账，会让正常用户被误降级。"""
    counter = FakeCounter()
    with pytest.raises(MixedModelError):
        _run(pinned=True, counter=counter, store=FakeStore(fail_insert=True))
    assert counter.recorded == 0


# ---------------- D2 图失败不阻断 ----------------


def test_graph_failure_does_not_break_write() -> None:
    result = _run(graph_writer=_bad_graph)
    assert result.created is True
    assert result.graph_ok is False


# ---------------- 参数与下游异常 ----------------


def test_bad_pinned_type_raises_guard_error() -> None:
    with pytest.raises(GuardError):
        _run(pinned="true")


def test_embedding_failure_propagates() -> None:
    def broken(_text: str, _settings) -> list[float]:
        raise EmbeddingError("服务挂了")

    with pytest.raises(EmbeddingError):
        _run(embedder=broken)


def test_event_id_stable_across_writes() -> None:
    """同样的摘要 + 日期 + namespace 必须给出同一 id（幂等地基）。"""
    a = _run()
    b = _run()
    assert a.event_id == b.event_id


def test_namespace_isolation() -> None:
    """不同 namespace 的同内容不能撞 id（ADR D1）。"""
    a = _run(namespace="work")
    b = _run(namespace="personal")
    assert a.event_id != b.event_id
