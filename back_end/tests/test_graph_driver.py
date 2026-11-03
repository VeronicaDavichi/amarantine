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
    assert count.written >= 0


def test_append_alias_edges_empty_is_noop(fake_neo4j: FakeDriver) -> None:
    """没有别名就不该连图。"""
    count = GO.append_alias_edges(
        "evt_0000000000000001",
        "work",
        [],
        settings=_Settings(),  # type: ignore[arg-type]
        created_at="2026-11-02T00:00:00+00:00",
    )
    assert count.written == 0
    assert fake_neo4j.uri is None, "空输入不该建 driver"


# ---------------- 别名参数必须带 key ----------------

def test_append_alias_edges_sends_key(fake_neo4j: FakeDriver) -> None:
    """`APPEND_ALIAS_CYPHER` 是 `MERGE (p {key: item.key})`。

    少了 `key` 就是 `{key: null}`，Neo4j 抛
    `Cannot merge the following node because of null property value`，
    异常见被 `except` 吞掉只留一行 warning —— **别名边一条都写不进去
    却看不出来**。这个洞真出过：`append_alias_edges` 漏了 `key`，
    而 `write_event_node` 有，所以只有推断路径坏。

    这里断言参数里带上了 key，而且跟 `key_for(canonical)` 一致。
    """
    from memory.core.entity_key import key_for

    GO.append_alias_edges(
        "evt_0000000000000001",
        "work",
        [_claim("糖包", "豆包")],
        settings=_Settings(),  # type: ignore[arg-type]
        created_at="2026-11-02T00:00:00+00:00",
        driver=fake_neo4j,
    )
    cypher, params = fake_neo4j.session_obj.calls[-1]
    assert "ALIAS_OF" in cypher
    aliases = params["aliases"]
    assert aliases, "应该有一个别名条目"
    for item in aliases:
        assert "key" in item, f"别名条目缺 key: {item}"
        assert item["key"], "key 不能为空 —— null 会让 MERGE 抛异常"
    assert aliases[0]["key"] == key_for("豆包")


def test_write_event_node_alias_items_carry_key() -> None:
    """`write_event_node` 那条路径本来就有 key —— 锁住别退化。

    不真跑 cypher（它需要完整的 _S 假对象），只检查源码里构造
    alias item 时带了 `key`。真库行为由 `verify_alias_guard.py` 验过。

    构造点从 `write_event_node` 挪到了 `_alias_item`（两条建边路径共用
    一份切分），所以断言跟着挪 —— 但要求没变。
    """
    import inspect

    src = inspect.getsource(GO._alias_item)
    assert '"key": key_for(claim.canonical)' in src, (
        "alias item 必须带 key，否则同时是 {key: null} → MERGE 抛异常"
    )
    # 两条路径都得走这个构造器，不能各写一份（否则拒绝理由会丢）
    for fn in (GO.write_event_node, GO.append_alias_edges):
        body = inspect.getsource(fn)
        assert "_partition_alias_claims" in body, (
            f"{fn.__name__} 必须走 _partition_alias_claims"
        )
