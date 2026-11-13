"""spec 048 T5：查询双向扩展（SAME_SENSE）。"""

from __future__ import annotations

import pytest

from memory.services import abbr as A


def test_expand_abbr_to_full() -> None:
    pairs = [("lysk", "恋与深空")]
    out, added = A.expand_query("lysk 周边有什么", pairs)
    assert "恋与深空" in out
    assert added == ["恋与深空"]


def test_expand_full_to_abbr() -> None:
    pairs = [("lysk", "恋与深空")]
    out, added = A.expand_query("恋与深空的周边", pairs)
    assert "lysk" in out
    assert added == ["lysk"]


def test_no_duplicate_when_both_present() -> None:
    pairs = [("lysk", "恋与深空")]
    out, added = A.expand_query("lysk 和恋与深空", pairs)
    assert added == []
    assert out == "lysk 和恋与深空"


def test_latin_boundary_not_mid_word() -> None:
    """`skyes` 里含 `sky`，不应触发 `sky` 的扩展。"""
    pairs = [("sky", "天空")]
    _out, added = A.expand_query("skyes 是什么", pairs)
    assert added == []


def test_expand_multiple() -> None:
    pairs = [("lysk", "恋与深空"), ("nn", "暖暖")]
    out, added = A.expand_query("lysk 和 nn", pairs)
    assert set(added) == {"恋与深空", "暖暖"}
    assert "恋与深空" in out and "暖暖" in out


def test_load_alias_pairs_degrades_without_graph() -> None:
    class BadDriver:
        def session(self):
            raise RuntimeError("neo4j down")

        def close(self):
            return None

    assert A.load_alias_pairs("qq-x", driver=BadDriver()) == []


def test_load_alias_pairs_returns_rows() -> None:
    class Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    class Result(list):
        pass

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def run(self, cypher, **params):
            return Result([Row(abbr="lysk", full="恋与深空")])

    class Driver:
        def session(self):
            return Session()

        def close(self):
            return None

    assert A.load_alias_pairs("ns", driver=Driver()) == [("lysk", "恋与深空")]


# ------------------------------------------------------------ recall 集成


def test_recall_uses_expanded_query(monkeypatch) -> None:
    """recall 的 embedding/实体抽取应看到扩展后的 query。"""
    from datetime import date, datetime, timezone

    from memory.services import recall as R
    from tests.test_recall import _hit, _run
    from memory.services.graph_search import GraphHits

    seen: list[str] = []

    def embedder(text: str, settings) -> list[float]:
        seen.append(text)
        return [0.01] * settings.EMBEDDING_DIM

    monkeypatch.setattr(A, "load_alias_pairs", lambda ns, **kw: [("lysk", "恋与深空")])
    monkeypatch.setattr(A, "expand_query", A.expand_query)

    result = _run(query="lysk 周边", embedder=embedder)
    assert result is not None
    assert any("恋与深空" in q for q in seen), seen
