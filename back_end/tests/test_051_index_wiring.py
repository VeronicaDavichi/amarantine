# -*- coding: utf-8 -*-
"""T3 接线测试：index_cards 与 run_ingest 的行为（用 fake store，不碰真库）。"""

from __future__ import annotations

import pytest

from memory.services import raw_text_index as R


class _FakeStore:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.calls = 0

    def set_raw_text_tokens(self, event_id: str, namespace: str, *, tokens: str) -> None:
        self.calls += 1
        self.written[event_id] = tokens


class _FakeRaw:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.asked: list[list[str]] = []

    def texts_for(self, namespace: str, quote_ids: list[str]) -> list[str]:
        self.asked.append(list(quote_ids))
        return self._texts


class _S:
    def __init__(self, enabled: bool = True, max_chars: int = 4000) -> None:
        self.RAW_TEXT_INDEX_ENABLED = enabled
        self.RAW_TEXT_INDEX_MAX_CHARS = max_chars
        self.RAW_MATERIAL_MAX_CHARS = 2000


def test_disabled_writes_nothing() -> None:
    store = _FakeStore()
    n = R.index_cards(namespace="ns", event_ids=["e1"], quote_ids=["q1"],
                      settings=_S(enabled=False), raw_store=_FakeRaw(["正常文本内容"]),
                      store=store)
    assert n == 0
    assert store.calls == 0


def test_enabled_writes_same_tokens_for_all_cards() -> None:
    """同一批卡共享 raw_quote_ids → token 串相同，算一次写 N 次。"""
    store = _FakeStore()
    raw = _FakeRaw(["上厕所不冲厕所的吗", "七点钟开始洗的现在还在转"])
    n = R.index_cards(namespace="ns", event_ids=["e1", "e2", "e3"],
                      quote_ids=["q1", "q2"], settings=_S(),
                      raw_store=raw, store=store)
    assert n == 3
    assert len(set(store.written.values())) == 1, "三张卡的 token 应完全一致"
    assert raw.asked == [["q1", "q2"]], "只查一次（禁止 N+1）"


def test_all_gated_writes_nothing() -> None:
    """整批都是营销/卡片 JSON → 不写（而不是写空串冒充已索引）。"""
    store = _FakeStore()
    raw = _FakeRaw(['{"app":"com.tencent.mannounce","config":{}}'])
    n = R.index_cards(namespace="ns", event_ids=["e1"], quote_ids=["q1"],
                      settings=_S(), raw_store=raw, store=store)
    assert n == 0
    assert store.calls == 0


def test_store_error_is_swallowed() -> None:
    """索引是增强项：写失败只告警，不抛。"""

    class _Boom(_FakeStore):
        def set_raw_text_tokens(self, *a, **kw):
            raise RuntimeError("weaviate down")

    n = R.index_cards(namespace="ns", event_ids=["e1"], quote_ids=["q1"],
                      settings=_S(), raw_store=_FakeRaw(["正常文本内容"]),
                      store=_Boom())
    assert n == 0


def test_empty_ids_short_circuit() -> None:
    store = _FakeStore()
    assert R.index_cards(namespace="ns", event_ids=[], quote_ids=["q1"],
                         settings=_S(), raw_store=_FakeRaw([]), store=store) == 0
    assert R.index_cards(namespace="ns", event_ids=["e1"], quote_ids=[],
                         settings=_S(), raw_store=_FakeRaw([]), store=store) == 0
