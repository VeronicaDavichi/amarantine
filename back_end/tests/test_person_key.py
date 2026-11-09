"""spec 045：Person 身份键（uid 优先）+ 改名不分裂。

覆盖 A1/A2（改名后 1 个 Person、旧名进 previous_names）、
A3（同名不同 uid → 2 个 Person）、A4（旧名可查询）、A6（无 uid 行为不变）。
"""

from __future__ import annotations

import pytest

from memory.services import identity as I
from memory.services import person_key as PK


@pytest.fixture(autouse=True)
def _clean_registry():
    PK.reset_registry()
    yield
    PK.reset_registry()


# ------------------------------------------------------------ 键解析


def test_explicit_uid_wins() -> None:
    """显式给 uid 时，键直接是 `uid:<uid>`，与名字无关。"""
    assert PK.resolve_person_key("王泳", uid="53660559") == "uid:53660559"
    assert PK.resolve_person_key("王泳2", uid="53660559") == "uid:53660559"


def test_registered_name_maps_to_uid() -> None:
    """只知道名字时靠登记表 —— 别名建边就是这条路。"""
    PK.register_name_uid({"王泳": "53660559"})
    assert PK.resolve_person_key("王泳") == "uid:53660559"


def test_unregistered_falls_back_to_name_key() -> None:
    """没有 uid 的写入（单条 remember）行为必须不变。"""
    from memory.core.entity_key import key_for

    assert PK.resolve_person_key("王泳") == key_for("王泳")


def test_register_is_idempotent() -> None:
    """重复登记同一对不重复计数。"""
    assert PK.register_name_uid({"王泳": "1"}) == 1
    assert PK.register_name_uid({"王泳": "1"}) == 0


def test_register_ignores_single_char_and_empty_uid() -> None:
    """单字名字和空 uid 都不收（单字会误伤普通词）。"""
    assert PK.register_name_uid({"王": "1", "李四": ""}) == 0
    assert PK.uid_for("王") is None


def test_registry_can_carry_multiple_names_of_one_uid() -> None:
    """同一个 uid 的多个名字都指向一个键（改名后旧名仍可解析）。"""
    PK.register_name_uid({"王泳": "1", "王泳2": "1"})
    assert PK.resolve_person_key("王泳") == PK.resolve_person_key("王泳2") == "uid:1"


# ------------------------------------------------------------ 账号层写入


class _Result:
    def consume(self) -> None:
        return None


class _Session:
    def __init__(self, driver: "_Driver") -> None:
        self.driver = driver

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, cypher: str, **params: object) -> _Result:
        self.driver.calls.append((cypher, params))
        return _Result()


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def session(self) -> _Session:
        return _Session(self)

    def close(self) -> None:
        return None


def test_account_write_uses_uid_key_and_records_previous_names() -> None:
    """A1/A2：Person 的键是 uid，改名时旧名进 previous_names。"""
    drv = _Driver()
    I.ensure_identities(
        namespace="ns1",
        identities=[I.Identity(uid="53660559", nick="王泳")],
        settings=object(),
        driver=drv,
    )
    cypher, params = drv.calls[0]
    assert params["key"] == "uid:53660559"
    assert "previous_names" in cypher, "改名必须把旧名留下来"
    assert "p.name = $display_name" in cypher


def test_same_nick_different_uid_gets_different_keys() -> None:
    """A3：同名不同 uid → 两个不同的身份键。"""
    drv = _Driver()
    I.ensure_identities(
        namespace="ns1",
        identities=[
            I.Identity(uid="111", nick="智小安业务助手"),
            I.Identity(uid="222", nick="智小安业务助手"),
        ],
        settings=object(),
        driver=drv,
    )
    keys = [params["key"] for _, params in drv.calls]
    assert keys == ["uid:111", "uid:222"]


def test_ensure_identities_registers_names_for_later_writes() -> None:
    """写入后登记名字→uid，同批卡片才不会又用名字键。"""
    drv = _Driver()
    I.ensure_identities(
        namespace="ns1",
        identities=[I.Identity(uid="53660559", nick="王泳")],
        settings=object(),
        driver=drv,
    )
    assert PK.resolve_person_key("王泳") == "uid:53660559"


# ------------------------------------------------------------ 查询兼容


def test_person_query_matches_previous_names() -> None:
    """A4：查询侧必须能按曾用名命中。"""
    from memory.services.graph_search import _ALL_NAMES_CYPHER, _SEARCH_CYPHER

    assert "previous_names" in _ALL_NAMES_CYPHER
    assert "previous_names" in _SEARCH_CYPHER


# ------------------------------------------------------------ 事件写入


def test_event_person_key_uses_registry() -> None:
    """事件卡的 person 实体走 uid 键（A1 的写入侧）。"""
    from memory.models.event_card import Entity
    from memory.services.graph_outbox import _split_entities

    class _Card:
        entities = [Entity(name="王泳", kind="person"), Entity(name="ESB", kind="topic")]

    PK.register_name_uid({"王泳": "53660559"})
    persons, entities = _split_entities(_Card())
    assert persons[0]["key"] == "uid:53660559"
    # 非人实体仍按名字派生（没有账号）
    assert entities[0]["key"].startswith("e_")
