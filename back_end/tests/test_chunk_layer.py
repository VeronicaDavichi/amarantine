"""A16：chunk 层集成测试（开关、聚合去重、降级）。

纯函数（切分、keep_top_n）在 `test_chunker.py` 里测。这里测**接线**：
chunk 命中如何并进召回、以及开关/降级行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_recall import FakeStore, _hit, _run


class _FakeChunkStore:
    """可配置的 `ChunkStore` 替身。"""

    hits: list[dict[str, Any]] = []
    boom: Exception | None = None

    def __init__(self, settings: Any) -> None:
        pass

    def search_near_vector(self, vector, namespace, *, limit=50):
        if _FakeChunkStore.boom is not None:
            raise _FakeChunkStore.boom
        return _FakeChunkStore.hits

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset():
    _FakeChunkStore.hits = []
    _FakeChunkStore.boom = None
    yield
    _FakeChunkStore.hits = []
    _FakeChunkStore.boom = None


def _settings(chunk_enabled: bool = True, keep: int = 2):
    from memory.config import load_settings

    s = load_settings()
    s.CHUNK_ENABLED = chunk_enabled
    s.CHUNK_KEEP_PER_EVENT = keep
    s.CHUNK_SIZE = 256
    s.CHUNK_OVERLAP = 32
    return s


def _patch(monkeypatch):
    import memory.services.recall as mod

    monkeypatch.setattr(mod, "ChunkStore", _FakeChunkStore)


def _hit_chunk(event_id: str, index: int, score: float) -> dict:
    return {
        "event_id": event_id,
        "chunk_index": index,
        "text": f"{event_id} 的第 {index} 块",
        "date_start": None,
        "score": score,
    }


# ---------------- 关闭时完全不影响 ----------------


def test_disabled_does_not_touch_chunk_store(monkeypatch) -> None:
    import memory.services.recall as mod

    called = {"n": 0}

    class _Spy(_FakeChunkStore):
        def __init__(self, settings):
            called["n"] += 1

    monkeypatch.setattr(mod, "ChunkStore", _Spy)
    result = _run(
        query="项目A的进度",
        settings=_settings(chunk_enabled=False),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
    )
    assert called["n"] == 0, "关掉时不该实例化 ChunkStore"
    assert result.trace["chunk_enabled"] is False
    assert len(result.materials) == 1


# ---------------- D1/D2：每卡保留 N 个 ----------------


def test_keeps_only_n_chunks_per_event(monkeypatch) -> None:
    _patch(monkeypatch)
    _FakeChunkStore.hits = [
        _hit_chunk("evt_long", 0, 0.95),
        _hit_chunk("evt_long", 1, 0.90),
        _hit_chunk("evt_long", 2, 0.85),
        _hit_chunk("evt_long", 3, 0.80),
        _hit_chunk("evt_long", 4, 0.75),
    ]
    result = _run(
        query="项目A的进度",
        settings=_settings(keep=2),
        store=FakeStore(vector_hits=[_hit("evt_long")], bm25_hits=[_hit("evt_long")]),
    )
    assert result.trace["chunk_hits"] == 5
    assert result.trace["chunk_parents"] == 2, "没按 CHUNK_KEEP_PER_EVENT 截断"


# ---------------- D3：chunk 不外泄给 LLM ----------------


def test_chunk_parent_appears_once_in_materials(monkeypatch) -> None:
    """materials 里是**卡**，不是 chunk —— 同一个 event_id 只能出现一次。"""
    _patch(monkeypatch)
    _FakeChunkStore.hits = [
        _hit_chunk("evt_long", 0, 0.95),
        _hit_chunk("evt_long", 1, 0.90),
        _hit_chunk("evt_long", 2, 0.85),
    ]
    result = _run(
        query="项目A的进度",
        settings=_settings(keep=2),
        store=FakeStore(
            vector_hits=[_hit("evt_long", "很长的项目A进展")],
            bm25_hits=[_hit("evt_long", "很长的项目A进展")],
        ),
    )
    ids = [m["event_id"] for m in result.materials]
    assert ids.count("evt_long") == 1, f"chunk 泄漏到 materials：{ids}"


# ---------------- 长卡能被 chunk 通道召回 ----------------


def test_chunk_only_parent_gets_recalled(monkeypatch) -> None:
    """向量/BM25 都没命中，但 chunk 命中 → 该卡仍被召回。"""
    _patch(monkeypatch)
    _FakeChunkStore.hits = [_hit_chunk("evt_hidden", 0, 0.93)]
    result = _run(
        query="项目A的进度",
        settings=_settings(keep=2),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
    )
    # chunk 通道把 evt_hidden 加了进去，但 meta 里没有它的内容，
    # 所以最终可能不出现在 materials —— 关键是**通道里有了**
    assert result.trace["chunk_parents"] == 1


# ---------------- 降级 ----------------


def test_chunk_store_failure_degrades(monkeypatch) -> None:
    _patch(monkeypatch)
    _FakeChunkStore.boom = RuntimeError("weaviate down")
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
    )
    assert result.trace["chunk_enabled"] is True
    assert result.trace["chunk_error"] == "RuntimeError"
    assert result.materials, "chunk 层挂了不该让召回变空"


def test_chunk_error_does_not_break_trace(monkeypatch) -> None:
    """降级时 trace 仍要有 top_k_decision —— 不丢可解释性。"""
    _patch(monkeypatch)
    _FakeChunkStore.boom = RuntimeError("down")
    result = _run(
        query="项目A的进度",
        settings=_settings(),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
    )
    assert result.trace["top_k_decision"]
