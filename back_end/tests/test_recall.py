"""C1/C5/C7 召回编排单测。

重点验证 ADR D3 的三条硬约束：
- Weaviate 挂了 → 503（`RecallUnavailableError`），不是空结果
- Neo4j 挂了 → 只 `degraded=true`，结果照常返回
- 空结果必须显式带 `empty_reason`，不能静默
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.config import load_settings
from memory.services.embedding import EmbeddingError
from memory.services.graph_search import GraphHits
from memory.services.recall import RecallUnavailableError, recall
from memory.services.weaviate_store import WeaviateStoreError

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 31)


class FakeStore:
    """内存版 Weaviate：可配置两路命中与失败。"""

    def __init__(
        self,
        *,
        vector_hits: list[dict] | None = None,
        bm25_hits: list[dict] | None = None,
        fail_vector: bool = False,
        fail_bm25: bool = False,
    ) -> None:
        self.vector_hits = vector_hits or []
        self.bm25_hits = bm25_hits or []
        self.fail_vector = fail_vector
        self.fail_bm25 = fail_bm25

    def search_near_vector(self, vector, namespace, *, limit=50, date_from=None):
        if self.fail_vector:
            raise WeaviateStoreError("weaviate down")
        return self.vector_hits

    def search_bm25(self, text, namespace, *, limit=50, date_from=None):
        if self.fail_bm25:
            raise WeaviateStoreError("weaviate down")
        return self.bm25_hits

    def close(self) -> None:
        pass


def _hit(eid: str, summary: str = "摘要", *, days_ago: int = 30, importance=0.5, pinned=False):
    from datetime import timedelta

    d = TODAY - timedelta(days=days_ago)
    return {
        "event_id": eid,
        "namespace": "work",
        "summary": summary,
        "date_start": f"{d.isoformat()}T00:00:00Z",
        "importance": importance,
        "pinned": pinned,
        "unresolved": [],
    }


def _embedder(text: str, settings) -> list[float]:
    return [0.01] * settings.EMBEDDING_DIM


def _graph_ok(entities, namespace, *, settings):
    return GraphHits(event_ids=[], ok=True)


def _graph_down(entities, namespace, *, settings):
    return GraphHits(event_ids=[], ok=False, error="neo4j down")


def _run(**overrides):
    settings = load_settings()
    kwargs = dict(
        query="上个月项目A的进展",
        namespace="work",
        settings=settings,
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
        embedder=_embedder,
        graph_searcher=_graph_ok,
        now=NOW,
        today=TODAY,
    )
    kwargs.update(overrides)
    return recall(**kwargs)


# ---------------- 正常路径 ----------------


def test_returns_materials_with_trace() -> None:
    result = _run()
    assert len(result.materials) == 1
    assert result.materials[0]["event_id"] == "evt_a"
    assert result.trace["empty_reason"] is None
    assert "top_k_decision" in result.trace
    assert result.degraded is False
    assert result.partial is False


def test_time_hint_parsed_from_query() -> None:
    """query 里的「上个月」要被摘出来，进 trace 可查。"""
    result = _run()
    assert result.trace["date_range"]["start"] == "2026-09-01"
    assert result.trace["date_range"]["matched"] == "上个月"


def test_rrf_merges_both_channels() -> None:
    """同时命中向量与关键词的卡，分数应高于只命中一路的。"""
    store = FakeStore(
        vector_hits=[_hit("evt_both"), _hit("evt_vec")],
        bm25_hits=[_hit("evt_both"), _hit("evt_kw")],
    )
    result = _run(store=store)
    ids = [m["event_id"] for m in result.materials]
    assert ids[0] == "evt_both"


def test_channel_sizes_reported() -> None:
    store = FakeStore(vector_hits=[_hit("a")], bm25_hits=[_hit("b")])
    result = _run(store=store)
    assert result.trace["channels"] == {"vector": 1, "bm25": 1}


# ---------------- 降级矩阵 ----------------


def test_graph_failure_degrades_but_returns_results() -> None:
    """Neo4j 挂了只标记 degraded，结果照常返回。"""
    result = _run(graph_searcher=_graph_down)
    assert result.degraded is True
    assert len(result.materials) == 1


def test_vector_failure_is_503_not_empty() -> None:
    """事实源挂了必须抛 503——空结果会让调用方以为「没有记忆」。"""
    with pytest.raises(RecallUnavailableError, match="vector"):
        _run(store=FakeStore(fail_vector=True))


def test_bm25_failure_is_503() -> None:
    with pytest.raises(RecallUnavailableError, match="bm25"):
        _run(store=FakeStore(vector_hits=[_hit("a")], fail_bm25=True))


def test_embedding_failure_is_503() -> None:
    def broken(_text: str, _settings):
        raise EmbeddingError("embedding down")

    with pytest.raises(RecallUnavailableError, match="embedding"):
        _run(embedder=broken)


# ---------------- 空结果（C5） ----------------


def test_no_candidate_is_explicit() -> None:
    result = _run(store=FakeStore())
    assert result.materials == []
    assert result.trace["empty_reason"] == "no_candidate"
    assert result.to_payload()["materials"] == []


def test_below_threshold_is_explicit() -> None:
    """有候选但最高分太低 → below_threshold，不拿低分凑数。"""
    settings = load_settings().model_copy(update={"RECALL_EMPTY_THRESHOLD": 0.99})
    result = _run(settings=settings, store=FakeStore(vector_hits=[_hit("weak")]))
    assert result.materials == []
    assert result.trace["empty_reason"] == "below_threshold"
    assert "top_score" in result.trace


# ---------------- 动态 K 与强制保留 ----------------


def test_many_candidates_are_truncated() -> None:
    hits = [_hit(f"evt_{i:02d}", days_ago=i) for i in range(30)]
    result = _run(store=FakeStore(vector_hits=hits))
    assert 1 <= len(result.materials) <= load_settings().RECALL_TOP_K_MAX
    assert result.trace["top_k_decision"]["reason"] in {
        "all", "gap", "budget", "clamp_max", "drop_ratio", "clamp_min",
    }


def test_pinned_survives_truncation() -> None:
    """pinned 是 D7 硬信号，必须进强制保留集。"""
    hits = [_hit(f"evt_n{i}", days_ago=i) for i in range(20)]
    hits.append(_hit("evt_pinned", days_ago=3000, pinned=True))
    result = _run(store=FakeStore(vector_hits=hits))
    ids = [m["event_id"] for m in result.materials]
    assert "evt_pinned" in ids
    assert result.materials[ids.index("evt_pinned")]["forced"] is True


def test_materials_carry_token_estimate() -> None:
    result = _run(store=FakeStore(vector_hits=[_hit("a", summary="张三决定项目A延期")]))
    assert result.materials[0]["tokens"] > 0


def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError, match="query"):
        _run(query="   ")
