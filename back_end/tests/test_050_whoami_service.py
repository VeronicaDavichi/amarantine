"""spec 050 延伸：whoami 服务（身份 + 我与别人的关系）。"""

from __future__ import annotations

import pytest

from memory.services import whoami as W


class _Settings:
    NEO4J_URI = "bolt://127.0.0.1:7687"
    NEO4J_USER = "neo4j"
    NEO4J_PASSWORD = "password"
    MEMORY_NAMESPACE = "ns1"


def test_relations_of_self_without_uid() -> None:
    rels, note = W.relations_of_self(_Settings(), "ns1", "")
    assert rels == []
    assert "没有解析出本人" in note


def test_relations_parsed(monkeypatch) -> None:
    rows = [
        {"row": ["OWNS", "服务器", "probable", 0.8]},
        {"row": ["MAINTAINS", "大肥鱼", "probable", 0.6]},
    ]
    monkeypatch.setattr(W, "_cy", lambda *a, **kw: rows)
    rels, note = W.relations_of_self(_Settings(), "ns1", "u_me")
    assert note == ""
    assert [r["predicate"] for r in rels] == ["OWNS", "MAINTAINS"]
    assert rels[0]["target"] == "服务器"


def test_graph_failure_degrades(monkeypatch) -> None:
    """图挂了不该让 whoami 失败 —— 身份信息仍然要给。"""
    def boom(*a, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(W, "_cy", boom)
    rels, note = W.relations_of_self(_Settings(), "ns1", "u_me")
    assert rels == []
    assert "图不可用" in note


def test_empty_relations_has_note(monkeypatch) -> None:
    monkeypatch.setattr(W, "_cy", lambda *a, **kw: [])
    rels, note = W.relations_of_self(_Settings(), "ns1", "u_me")
    assert rels == []
    assert "还没有" in note


def test_build_whoami_shape(monkeypatch) -> None:
    """`build_whoami` 要同时给身份与关系，字段名稳定。"""
    monkeypatch.setattr(W, "relations_of_self", lambda *a, **kw: ([], "none"))
    monkeypatch.setattr(
        "memory.services.self_identity.resolve_self",
        lambda s, namespace=None: {"configured": False, "qq": "", "uid": "",
                                   "names": [], "canonical": "", "reason": "没配"},
    )
    got = W.build_whoami(_Settings(), namespace="ns1")
    assert set(got) == {"identity", "namespace", "relations", "relations_note"}
    assert got["namespace"] == "ns1"
