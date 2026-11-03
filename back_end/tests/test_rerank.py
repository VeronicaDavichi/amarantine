"""A27：rerank 精排 + 降级路径。

ADR 原文：kill rerank 服务，`POST /admin/recall` 仍返回 200，
`trace.rerank_skipped=true`、`trace.rerank_ms=0`，材料集非空。

**重点是降级，不是调通**：rerank 是增强项，不能成为可用性的单点。
"""

from __future__ import annotations

import httpx
import pytest

from memory.config import load_settings
from memory.services.rerank import RerankResult, rerank
from tests.test_recall import FakeStore, _hit, _run


class _FakeResp:
    def __init__(self, status: int, data) -> None:
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class _FakeClient:
    """可配置的 httpx 替身。"""

    def __init__(self, *, status: int = 200, data=None, boom=None) -> None:
        self.status = status
        self.data = data
        self.boom = boom
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.boom is not None:
            raise self.boom
        return _FakeResp(self.status, self.data)

    def close(self) -> None:
        pass


def _settings(**overrides):
    s = load_settings()
    s.RERANK_ENABLED = True
    s.RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
    s.RERANK_BASE_URL = "http://127.0.0.1:8083"
    s.RERANK_TIMEOUT_MS = 300
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ---------------- 单元：rerank() 本身 ----------------


def test_rerank_maps_scores_by_index() -> None:
    client = _FakeClient(data=[{"index": 0, "score": 0.1}, {"index": 1, "score": 0.9}])
    result = rerank(
        "q",
        [("evt_a", "无关"), ("evt_b", "很相关")],
        settings=_settings(),
        client=client,
    )
    assert result.skipped is False
    assert result.scores == {"evt_a": 0.1, "evt_b": 0.9}
    assert result.elapsed_ms >= 0


def test_request_shape_matches_tei() -> None:
    """TEI 要的是 `query` + `texts` + `model`，且 URL 是 `/rerank`。"""
    client = _FakeClient(data=[{"index": 0, "score": 1.0}])
    rerank("q", [("evt_a", "文本")], settings=_settings(), client=client)
    call = client.calls[0]
    assert call["url"].endswith("/rerank")
    assert call["json"]["query"] == "q"
    assert call["json"]["texts"] == ["文本"]
    assert call["json"]["model"] == "BAAI/bge-reranker-v2-m3"


@pytest.mark.parametrize(
    "boom",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timeout"),
        RuntimeError("boom"),
    ],
)
def test_rerank_never_raises(boom) -> None:
    """任何异常都归成 `scores=None`，绝不向上抛。"""
    result = rerank(
        "q", [("evt_a", "x")], settings=_settings(), client=_FakeClient(boom=boom)
    )
    assert result.skipped is True
    assert result.scores is None
    assert result.elapsed_ms == 0, "跳过时 rerank_ms 必须是 0（ADR 明文）"


def test_non_200_becomes_skipped() -> None:
    """424（模型不是 reranker）也走降级 —— 服务「在」但「不可用」。"""
    result = rerank(
        "q", [("evt_a", "x")], settings=_settings(), client=_FakeClient(status=424)
    )
    assert result.skipped is True
    assert result.reason == "http_424"


def test_disabled_short_circuits_without_http() -> None:
    client = _FakeClient(data=[])
    result = rerank(
        "q", [("evt_a", "x")], settings=_settings(RERANK_ENABLED=False), client=client
    )
    assert result.skipped is True
    assert result.reason == "disabled"
    assert client.calls == [], "关掉时不该发请求"


# ---------------- 集成：recall 的 trace ----------------


def _hits():
    return [_hit("evt_a", "项目A延期到六月"), _hit("evt_b", "项目A又延期到七月")]


def test_recall_reranks_and_traces() -> None:
    client = _FakeClient(data=[{"index": 0, "score": 0.2}, {"index": 1, "score": 0.9}])
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
        rerank_client=client,
    )
    assert result.trace["rerank_skipped"] is False
    assert result.trace["rerank_ms"] >= 0
    assert len(result.materials) >= 1


def test_recall_survives_rerank_down() -> None:
    """A27 核心断言：服务挂了，召回仍 200，材料非空。"""
    client = _FakeClient(boom=httpx.ConnectError("refused"))
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
        rerank_client=client,
    )
    assert result.trace["rerank_skipped"] is True
    assert result.trace["rerank_ms"] == 0
    assert result.materials, "降级后材料集不能为空"


def test_trace_keeps_top_k_on_degrade() -> None:
    """降级不能丢可解释性：`top_k_decision` 必须还在。"""
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
        rerank_client=_FakeClient(boom=RuntimeError("down")),
    )
    assert "top_k_decision" in result.trace
    assert result.trace["top_k_decision"]


def test_disabled_behaves_like_down() -> None:
    a = _run(
        query="项目A的进度",
        settings=_settings(RERANK_ENABLED=False),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
    )
    b = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
        rerank_client=_FakeClient(boom=RuntimeError("down")),
    )
    assert a.trace["rerank_skipped"] is b.trace["rerank_skipped"] is True
    assert a.trace["rerank_ms"] == b.trace["rerank_ms"] == 0
    assert [m["event_id"] for m in a.materials] == [m["event_id"] for m in b.materials]


def test_rerank_reorders_candidates() -> None:
    """rerank 分高的排前面 —— 这是 rerank 存在的意义。"""
    client = _FakeClient(
        data=[{"index": 0, "score": 0.1}, {"index": 1, "score": 0.99}]
    )
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=_hits(), bm25_hits=_hits()),
        rerank_client=client,
    )
    ids = [m["event_id"] for m in result.materials]
    assert ids[0] == "evt_b", f"rerank 没生效，顺序是 {ids}"
