"""019：`RECALL_BUDGET_MS` 真的限制总耗时。

**这个文件必须用真实 sleep，不能用 mock。**

超时逻辑只能靠真实时间验证：如果只用「返回很快的假通道」，
预算不生效也测不出来 —— 这正是这个 bug 存在很久没被发现的原因。

两个容易踩的坑：
1. `with ThreadPoolExecutor(...)` 退出时是 `shutdown(wait=True)`，会把慢线程等到结束。
   所以测试必须能区分「wait 生效」和「只是 wait 生效但退出还在等」。
2. 第一次调用包含 jieba 加载 + TEI 建连（约 1-2 秒），
   会把耗时撑爆、让测试误判。必须先 warm。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from memory.config import load_settings
from memory.services.graph_search import GraphHits
from memory.services.recall import recall

BUDGET_MS = 300
SLOW_S = 1.0
#: 预算 + 余量。慢通道是 1 秒，没修好的话必然 > 1000ms，能清楚区分。
ALLOWED_MS = BUDGET_MS + 400


class SlowStore:
    """向量和 BM25 可以设不同的延迟，方便构造「一个快两个慢」。"""

    def __init__(
        self,
        seconds: float,
        hits: list[dict] | None = None,
        *,
        bm25_seconds: float | None = None,
    ) -> None:
        self.seconds = seconds
        self.bm25_seconds = seconds if bm25_seconds is None else bm25_seconds
        self.hits = hits or []
        self.bm25_hits: list[dict] = []

    def search_near_vector(self, *a, **kw):
        time.sleep(self.seconds)
        return list(self.hits)

    def search_bm25(self, *a, **kw):
        time.sleep(self.bm25_seconds)
        return list(self.bm25_hits)

    def close(self) -> None:
        pass


def make_graph(seconds: float, ids: list[str] | None = None):
    def f(*a, **kw) -> GraphHits:
        time.sleep(seconds)
        return GraphHits(event_ids=list(ids or []))

    return f


def _settings():
    s = load_settings()
    s.RECALL_BUDGET_MS = BUDGET_MS
    s.RERANK_ENABLED = False
    s.CHUNK_ENABLED = False
    return s


def _timed(settings, store, graph):
    t0 = time.perf_counter()
    result = recall(
        "项目A",
        namespace="work",
        settings=settings,
        store=store,
        graph_searcher=graph,
    )
    return (time.perf_counter() - t0) * 1000, result


@pytest.fixture(scope="module", autouse=True)
def _warm() -> None:
    """把 jieba / httpx / TEI 的一次性开销挪出测量窗口。"""
    _timed(_settings(), SlowStore(0.0), make_graph(0.0))


def test_budget_caps_total_time() -> None:
    """三通道各慢 1s，预算 300ms → 总耗时必须远小于 1s。"""
    ms, result = _timed(_settings(), SlowStore(SLOW_S), make_graph(SLOW_S))
    assert ms < ALLOWED_MS, (
        f"耗时 {ms:.0f}ms 超过 {ALLOWED_MS}ms —— "
        f"预算没生效，或 shutdown(wait=True) 把慢线程等完了"
    )
    assert result.partial is True


def test_all_channels_timeout_is_degraded_not_503() -> None:
    """三通道全部超时 → 空结果 + degraded + budget_exhausted，**不抛 503**。

    超时不等于服务挂：真的挂走 WeaviateStoreError → 503。
    """
    _, result = _timed(_settings(), SlowStore(SLOW_S), make_graph(SLOW_S))
    assert result.materials == []
    assert result.degraded is True
    assert result.partial is True
    assert result.trace["empty_reason"] == "budget_exhausted"
    assert set(result.trace["errors"]) == {"vector", "bm25", "graph"}


def test_every_timed_out_channel_is_reported() -> None:
    """三个通道都超时，`errors` 里必须三个都在。

    旧实现只用 `remaining`（从不递减）顺序等，只有第一个撞线的通道会被标，
    后面的通道「借」到时间就不标了 —— 所以 `partial` 曾经是不准的。
    """
    _, result = _timed(_settings(), SlowStore(SLOW_S), make_graph(SLOW_S))
    assert "vector" in result.trace["errors"]
    assert "bm25" in result.trace["errors"]
    assert "graph" in result.trace["errors"]


def test_fast_channel_results_survive_slow_peers() -> None:
    """BM25 快且有结果，向量和图超时 → BM25 的结果必须保留。

    注意：图通道命中的 id 没有元数据（P0 限制，trace 里会写 note），
    所以拿它验证「结果保留」不成立。用 BM25 的完整命中来验。
    """
    hit = {
        "event_id": "evt_fast",
        "summary": "快的通道命中",
        "score": 0.9,
        "date_start": "2026-06-15",
        "namespace": "work",
        "entities": [],
        "types": ["work"],
        "retention": "normal",
        "ingested_at": datetime(2026, 6, 15, tzinfo=timezone.utc).isoformat(),
        "importance": 0.5,
    }
    settings = _settings()
    store = SlowStore(SLOW_S, bm25_seconds=0.0)  # 向量慢、BM25 快
    store.bm25_hits = [hit]
    ms, result = _timed(settings, store, make_graph(SLOW_S))  # graph 也慢
    assert ms < ALLOWED_MS, f"耗时 {ms:.0f}ms"
    assert result.partial is True
    assert "vector" in result.trace["errors"]
    assert result.materials, "快通道（BM25）的结果被丢掉了"
    assert result.materials[0]["event_id"] == "evt_fast"


def test_fast_path_unaffected() -> None:
    """全快时不该有 partial / degraded，也不该被预算逻辑拖慢。"""
    ms, result = _timed(_settings(), SlowStore(0.0), make_graph(0.0))
    assert result.partial is False
    assert result.degraded is False
    assert result.trace["errors"] == {}
    assert ms < ALLOWED_MS
