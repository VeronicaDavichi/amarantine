"""图服务的 driver 构造路径。

`list_known_names` / `append_alias_edges` / `search_by_entities` 都有两条
driver 路径：注入的（测试用）和自建的（生产用）。测试全都注入 driver，
所以**自建那条从来没被跑到过** —— 于是漏了 `from neo4j import
GraphDatabase`，生产一调就 `NameError`。

这组测试专门走自建路径。`neo4j.GraphDatabase` 被 monkeypatch 成假对象，
不连真库。
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.services import graph_outbox as GO


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, cypher: str, **params: Any) -> Any:
        self.calls.append((cypher, params))

        class _Result:
            def __iter__(self):
                return iter([])

            def consume(self):
                return None

        return _Result()


class FakeDriver:
    def __init__(self) -> None:
        self.session_obj = FakeSession()
        self.closed = False
        self.uri: str | None = None

    def session(self) -> FakeSession:
        return self.session_obj

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_neo4j(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    """把 `neo4j.GraphDatabase.driver` 换成返回假 driver。"""
    import neo4j

    drv = FakeDriver()

    def _driver(uri: str, **kwargs: Any) -> FakeDriver:
        drv.uri = uri
        return drv

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", _driver)
    return drv


class _Settings:
    NEO4J_URI = "bolt://fake:7687"
    NEO4J_USER = "neo4j"
    NEO4J_PASSWORD = "pw"


# ---------------- list_known_names ----------------

def test_list_known_names_builds_its_own_driver(fake_neo4j: FakeDriver) -> None:
    """不注入 driver 时，生产路径必须能自己建。

    这条以前是 `NameError: name 'GraphDatabase' is not defined` ——
    因为 `GraphDatabase` 只在函数里用到，模块顶部没 import。
    """
    GO.list_known_names("work", settings=_Settings())  # type: ignore[arg-type]
    assert fake_neo4j.uri == "bolt://fake:7687"
    assert fake_neo4j.session_obj.calls, "应该真的发了查询"


def test_list_known_names_closes_own_driver(fake_neo4j: FakeDriver) -> None:
    GO.list_known_names("work", settings=_Settings())  # type: ignore[arg-type]
    assert fake_neo4j.closed, "自建的 driver 要负责关掉"


def test_list_known_names_does_not_close_injected_driver() -> None:
    drv = FakeDriver()
    GO.list_known_names("work", settings=_Settings(), driver=drv)  # type: ignore[arg-type]
    assert not drv.closed, "注入的 driver 由调用方管"


# ---------------- append_alias_edges ----------------

def _claim(alias: str = "糖包", canonical: str = "豆包"):
    from memory.models.event_card import AliasClaim

    return AliasClaim(alias=alias, canonical=canonical, label="probable")


def test_append_alias_edges_builds_its_own_driver(
    fake_neo4j: FakeDriver,
) -> None:
    """同一条路径，`append_alias_edges` 也漏了 import。"""
    count = GO.append_alias_edges(
        "evt_0000000000000001",
        "work",
        [_claim()],
        settings=_Settings(),  # type: ignore[arg-type]
        created_at="2026-11-02T00:00:00+00:00",
    )
    assert fake_neo4j.uri == "bolt://fake:7687"
    assert count >= 0


def test_append_alias_edges_empty_is_noop(fake_neo4j: FakeDriver) -> None:
    """没有别名就不该连图。"""
    count = GO.append_alias_edges(
        "evt_0000000000000001",
        "work",
        [],
        settings=_Settings(),  # type: ignore[arg-type]
        created_at="2026-11-02T00:00:00+00:00",
    )
    assert count == 0
    assert fake_neo4j.uri is None, "空输入不该建 driver"
