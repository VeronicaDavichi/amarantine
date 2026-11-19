"""spec 050：命名空间清理（三处一起删 + 确认语 + dry-run）。"""

from __future__ import annotations

import pytest

from memory.services import purge as P


def test_confirm_phrase_format() -> None:
    assert P.confirm_phrase("work") == "purge work"


@pytest.mark.parametrize("bad", ["", "  ", "*", "a,b", "two words"])
def test_namespace_must_be_literal(bad: str) -> None:
    """拒绝通配/列表/空 —— 防止一次删掉多个域。"""
    with pytest.raises(P.PurgeError):
        P._check_namespace(bad)


def test_confirm_required_for_apply(tmp_path, monkeypatch) -> None:
    """真删时确认语必须一字不差。"""
    settings = _fake_settings(tmp_path)
    monkeypatch.setattr(P, "count_namespace", lambda s, ns: {"cards": 0})
    with pytest.raises(P.PurgeError, match="确认语不匹配"):
        P.purge_namespace(settings, "work", confirm="purge works", apply=True)


def test_dry_run_does_not_require_confirm(tmp_path, monkeypatch) -> None:
    """预演不改数据，不该被确认语挡住（否则「看看会删多少」都做不到）。"""
    settings = _fake_settings(tmp_path)
    monkeypatch.setattr(P, "count_namespace", lambda s, ns: {"cards": 3})
    report = P.purge_namespace(settings, "work", confirm="", apply=False)
    assert report["applied"] is False
    assert report["before"]["cards"] == 3
    assert report["ok"] is True


def test_apply_reports_residual(tmp_path, monkeypatch) -> None:
    """删完还有残留要报出来（不能假装成功）。"""
    settings = _fake_settings(tmp_path)

    counts = iter([{"cards": 2}, {"cards": 1}])
    monkeypatch.setattr(P, "count_namespace", lambda s, ns: next(counts))
    monkeypatch.setattr(P, "_cy", lambda *a, **kw: [[1]])
    monkeypatch.setattr(P, "_gql", lambda *a, **kw: {"data": {"Get": {"MemoryEvent": []}}})
    monkeypatch.setattr(P, "_db_path", lambda s: tmp_path / "t.db")

    report = P.purge_namespace(settings, "work", confirm="purge work", apply=True)
    assert report["applied"] is True
    assert report["ok"] is False, "复核发现残留时 ok 必须为 False"
    assert report["residual"] == {"cards": 1}


def _fake_settings(tmp_path):
    class _S:
        NEO4J_URI = "bolt://127.0.0.1:7687"
        NEO4J_USER = "neo4j"
        NEO4J_PASSWORD = "password"
        WEAVIATE_URL = "http://127.0.0.1:8081"
        DATABASE_URL = f"sqlite:///{tmp_path / 't.db'}"

    return _S()
