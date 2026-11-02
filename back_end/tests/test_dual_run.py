"""B5 双跑复核接线测试。

规则层与 LLM 层各打一次标，分歧进 `LABEL_REVIEW` 队列。

**最重要的一条**：写卡用的必须永远是规则层结果。
`types` 参与 `event_id` 哈希（D1），若让 LLM 改写 types，
同一条事件重写会得到不同 event_id —— 幂等直接失效。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.config import Settings
from memory.core.event_id import make_event_id
from memory.services.embedding import EmbeddingError
from memory.services.graph_outbox import OutboxResult
from memory.services.label_review import LabelReviewQueue
from memory.services.labeler import LabelResult, label_by_rules
from memory.services.remember import remember_event
from memory.services.weaviate_store import to_properties

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 31)
SUMMARY = "张三决定项目A延期到六月"


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict = {}

    def ensure_collection(self) -> None:
        pass

    def get_by_event_id(self, namespace, event_id):
        return self.rows.get((namespace, event_id))

    def insert_event(self, card, vector, *, anomaly=None) -> str:
        self.rows[(card.namespace, card.event_id)] = to_properties(card, anomaly=anomaly)
        return f"uuid-{card.event_id}"

    def close(self) -> None:
        pass


class FakeCounter:
    def count_pinned(self, namespace, *, now):
        return 0

    def record_pinned(self, namespace, *, now):
        pass


def _embedder(text, settings):
    return [0.01] * settings.EMBEDDING_DIM


def _ok_graph(card, *, settings, weaviate_id, context=None):
    return OutboxResult(ok=True)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.MEMORY_NAMESPACE = "work"
    s.DATABASE_URL = f"sqlite:///{(tmp_path / 'q.sqlite3').as_posix()}"
    s.EMBEDDING_DIM = 8
    return s


def _queue(settings: Settings) -> LabelReviewQueue:
    return LabelReviewQueue(settings.DATABASE_URL.split("///", 1)[-1])


def _run(settings, **overrides):
    kwargs = dict(
        namespace="work",
        summary=SUMMARY,
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


def _fixed_labeler(types: list[str]):
    """返回固定 types 的假 LLM。"""

    def _call(summary, candidates):
        return LabelResult(types=list(types), source="llm")

    return _call


# ---------------- 一致：不入队 ----------------


def test_agreement_does_not_enqueue(settings) -> None:
    rules = label_by_rules(SUMMARY)
    _run(settings, llm_labeler=_fixed_labeler(list(rules.types)))
    assert _queue(settings).count_pending("work") == 0


def test_llm_none_result_does_not_enqueue(settings) -> None:
    """LLM 放弃（返回 None）不算分歧 —— 那只是没意见。"""
    _run(settings, llm_labeler=lambda s, c: None)
    assert _queue(settings).count_pending("work") == 0


def test_llm_exception_does_not_break_write(settings) -> None:
    """LLM 挂了，卡照写，队列不脏。"""

    def boom(summary, candidates):
        raise RuntimeError("网关超时")

    result = _run(settings, llm_labeler=boom)
    assert result.created is True
    assert _queue(settings).count_pending("work") == 0


# ---------------- 分歧：入队 ----------------


def test_disagreement_enqueues(settings) -> None:
    _run(settings, llm_labeler=_fixed_labeler(["study", "emotion"]))
    q = _queue(settings)
    assert q.count_pending("work") == 1
    item = q.list_pending("work")[0]
    assert item.agreement < settings.LABEL_DUAL_RUN_JACCARD
    assert item.llm_types == ["study", "emotion"]
    assert item.summary == SUMMARY


def test_enqueued_event_id_matches_written_card(settings) -> None:
    """队列里的 event_id 必须能对上真正写进去的那张卡，否则人工无从下手。"""
    store = FakeStore()
    result = _run(
        settings, store=store, llm_labeler=_fixed_labeler(["study", "emotion"])
    )
    item = _queue(settings).list_pending("work")[0]
    assert item.event_id == result.event_id
    assert (("work"), result.event_id) in store.rows


# ---------------- 开关 ----------------


def test_dual_run_off_skips_llm(settings) -> None:
    """LABEL_DUAL_RUN=False 时不该调 LLM（省一次网络往返）。"""
    settings.LABEL_DUAL_RUN = False
    calls = []

    def spy(summary, candidates):
        calls.append(summary)
        return LabelResult(types=["study"], source="llm")

    _run(settings, llm_labeler=spy)
    assert calls == []
    assert _queue(settings).count_pending("work") == 0


def test_no_labeler_skips_queue(settings) -> None:
    _run(settings)
    assert _queue(settings).count_pending("work") == 0


def test_jaccard_threshold_is_configurable(settings) -> None:
    """阈值调低到 0 后，任何结果都算一致。"""
    settings.LABEL_DUAL_RUN_JACCARD = 0.0
    _run(settings, llm_labeler=_fixed_labeler(["study", "emotion"]))
    assert _queue(settings).count_pending("work") == 0


# ---------------- 铁律：types 不受 LLM 影响 ----------------


def test_event_id_ignores_llm_types(settings) -> None:
    """**核心不变量**：LLM 说什么都不影响 event_id。"""
    a = _run(settings, llm_labeler=_fixed_labeler(["study"]))
    b = _run(settings, llm_labeler=_fixed_labeler(["health", "money"]))
    c = _run(settings)  # 不打标

    assert a.event_id == b.event_id == c.event_id


def test_written_card_uses_rule_types(settings) -> None:
    store = FakeStore()
    _run(
        settings,
        store=store,
        llm_labeler=_fixed_labeler(["study", "emotion"]),
    )
    props = next(iter(store.rows.values()))
    rules = label_by_rules(SUMMARY)
    assert props["types"] == rules.types, "写卡的 types 必须是规则层结果"


def test_rewrite_is_idempotent_with_llm(settings) -> None:
    """带 LLM 重写两次 → 仍然只一份。"""
    store = FakeStore()
    first = _run(settings, store=store, llm_labeler=_fixed_labeler(["study"]))
    second = _run(settings, store=store, llm_labeler=_fixed_labeler(["health"]))
    assert first.created is True
    assert second.created is False
    assert first.event_id == second.event_id
    assert len(store.rows) == 1


# ---------------- 队列是旁路，不能反噬 ----------------


def test_queue_failure_does_not_break_write(settings, monkeypatch) -> None:
    """队列写不进去也不该让「记住」失败。"""
    import memory.services.remember as mod

    def boom(_settings):
        raise RuntimeError("数据库锁死")

    monkeypatch.setattr(
        "memory.services.label_review.queue_from_settings", boom, raising=False
    )
    result = _run(settings, llm_labeler=_fixed_labeler(["study", "emotion"]))
    assert result.created is True


def test_repeated_disagreement_upserts_once(settings) -> None:
    """同一条反复写 → 队列里只有一行（靠 event_id 幂等）。"""
    for _ in range(3):
        _run(settings, llm_labeler=_fixed_labeler(["study", "emotion"]))
    assert _queue(settings).count_pending("work") == 1
