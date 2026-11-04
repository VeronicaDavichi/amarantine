"""017：`VECTOR_TIMEOUT_MS` 真的决定向量通道的 HTTP 超时。

分两层验证：

1. **store 层**：`search_near_vector(timeout_s=0.8)` 是否真的让 httpx 用 0.8 秒。
   httpx 把超时放进 `request.extensions["timeout"]`，可以在 MockTransport 里读到。
2. **recall 层**：`settings.VECTOR_TIMEOUT_MS` 是否被换算成秒传下去。

只验一层不够：store 层能过但 recall 不传配置 = 配置仍是装饰品；
recall 层能过但 store 忽略参数 = 传了也白传。
"""

from __future__ import annotations

import httpx
import pytest

from memory.config import Settings
from memory.services.weaviate_store import COLLECTION, WeaviateStore
from tests.test_recall import FakeStore, _hit, _run


# ---------------- store 层：per-request 超时真的到了 httpx ----------------

class _GraphQLRecorder:
    """只回答 GraphQL 查询，记录每次请求实际生效的超时。"""

    def __init__(self) -> None:
        self.timeouts: list[dict | None] = []
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # ensure_collection 会先探测 schema，那次不带我们的超时
        if request.url.path == "/v1/schema":
            return httpx.Response(200, json={"classes": [{"class": COLLECTION}]})
        if request.url.path.startswith("/v1/schema/"):
            return httpx.Response(200, json={"properties": []})
        if request.url.path == "/v1/graphql":
            self.timeouts.append(request.extensions.get("timeout"))
            self.paths.append(request.url.path)
            return httpx.Response(200, json={"data": {"Get": {"MemoryEvent": []}}})
        return httpx.Response(200, json={})


def _settings() -> Settings:
    return Settings(
        MEMORY_NAMESPACE="work",
        WEAVIATE_URL="http://weaviate.test",
        EMBEDDING_DIM=4,
    )


def _store(recorder: _GraphQLRecorder) -> WeaviateStore:
    # 不显式传 timeout，让 client 用 httpx 默认值（5s），
    # 这样「None → 沿用 client」和「0.8 → 覆盖」能区分开
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return WeaviateStore(_settings(), client=client)


def test_search_near_vector_passes_timeout_to_httpx() -> None:
    rec = _GraphQLRecorder()
    _store(rec).search_near_vector([0.0] * 4, "work", timeout_s=0.8)
    assert rec.timeouts, "压根没发 GraphQL"
    assert rec.timeouts[-1]["read"] == pytest.approx(0.8), (
        f"httpx 实际用的是 {rec.timeouts[-1]}"
    )


def test_search_near_vector_without_timeout_uses_client_default() -> None:
    """不传时不能把 client 的超时改掉 —— 那会误伤 list_all 和写入。"""
    rec = _GraphQLRecorder()
    _store(rec).search_near_vector([0.0] * 4, "work")
    assert rec.timeouts[-1]["read"] == pytest.approx(5.0), (
        f"应该沿用 client 默认 5s，实际 {rec.timeouts[-1]}"
    )


def test_list_all_is_not_affected() -> None:
    """`list_all` 走同一个 `_graphql`，但绝不能拿到通道超时。"""
    rec = _GraphQLRecorder()
    _store(rec).list_all("work")
    assert rec.timeouts[-1]["read"] == pytest.approx(5.0), (
        f"list_all 被误伤了：{rec.timeouts[-1]}"
    )


def test_bm25_is_not_affected() -> None:
    """ADR 只定义了向量通道超时，BM25 不该套同一个值。"""
    rec = _GraphQLRecorder()
    _store(rec).search_bm25("项目A", "work")
    assert rec.timeouts[-1]["read"] == pytest.approx(5.0), (
        f"BM25 被误伤了：{rec.timeouts[-1]}"
    )


# ---------------- recall 层：配置值真的被换算并传下去 ----------------

def _store_with_hit() -> FakeStore:
    hits = [_hit("evt_a")]
    return FakeStore(vector_hits=hits, bm25_hits=hits)


def _settings_for_recall(vector_timeout_ms: int):
    from memory.config import load_settings

    s = load_settings()
    s.RERANK_ENABLED = False
    s.CHUNK_ENABLED = False
    s.VECTOR_TIMEOUT_MS = vector_timeout_ms
    return s


@pytest.mark.parametrize(
    ("ms", "expected_s"),
    [(800, 0.8), (1200, 1.2), (50, 0.05)],
)
def test_recall_converts_config_to_seconds(ms: int, expected_s: float) -> None:
    store = _store_with_hit()
    _run(query="项目A", settings=_settings_for_recall(ms), store=store)
    assert store.vector_timeouts, "没调向量通道"
    assert store.vector_timeouts[0] == pytest.approx(expected_s), (
        f"VECTOR_TIMEOUT_MS={ms} 应换算成 {expected_s}s"
    )


def test_zero_means_unlimited_not_instant_fail() -> None:
    """`0` = 关闭该闸门 → 传 None。传 0 的话 httpx 会当成「立即超时」，语义相反。"""
    store = _store_with_hit()
    _run(query="项目A", settings=_settings_for_recall(0), store=store)
    assert store.vector_timeouts[0] is None, (
        f"0 应该是 None（不限），实际传了 {store.vector_timeouts[0]}"
    )


def test_default_config_value_is_used() -> None:
    """不显式改配置时，默认 800 应该生效（不是 None）。"""
    from memory.config import load_settings

    s = load_settings()
    s.RERANK_ENABLED = False
    s.CHUNK_ENABLED = False
    store = _store_with_hit()
    _run(query="项目A", settings=s, store=store)
    assert store.vector_timeouts[0] == pytest.approx(s.VECTOR_TIMEOUT_MS / 1000.0)
