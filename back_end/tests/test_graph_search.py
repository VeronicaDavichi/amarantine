"""C1 图通道：人跟实体分流。

核心规则（这一轮的需求）：

- **查人** → 直连命中就收工，不碰动作
- **查实体** → 命中后经事件的人/动作再扩一层

人是稀有枢纽，名字出现本身就是强信号，再加动作只会引入噪声。
实体被提过很多次，光看字面提到它的事件召回不了「本该相关但没提它」的事。
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.services.graph_search import (
    _SEARCH_CYPHER,
    GraphHits,
    search_by_entities,
)


class FakeRow(dict):
    def get(self, key: str, default: Any = None) -> Any:
        return dict.get(self, key, default)


class FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def __iter__(self):
        for row in self._rows:
            yield FakeRow(event_id=row)


class FakeAliasResult:
    """别名查询返回结构跟主查询不一样：带 label / confidence。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __iter__(self):
        for row in self._rows:
            yield FakeRow(row)


class FakeSession:
    def __init__(
        self, rows: list[str], aliases: list[dict[str, Any]] | None = None
    ) -> None:
        self._rows = rows
        self._aliases = aliases or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, cypher: str, **params: Any) -> Any:
        self.calls.append((cypher, params))
        # 别名解析是单独一次查询（RESOLVE_ALIAS_CYPHER），结构不同
        if "ALIAS_OF" in cypher:
            return FakeAliasResult(self._aliases)
        return FakeResult(self._rows)


class FakeDriver:
    def __init__(
        self, rows: list[str], aliases: list[dict[str, Any]] | None = None
    ) -> None:
        self.session_obj = FakeSession(rows, aliases)

    def session(self) -> FakeSession:
        return self.session_obj

    def close(self) -> None:
        pass


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver(["evt_a", "evt_b"])


def _search(drv: FakeDriver, entities, persons=None, **kw) -> GraphHits:
    return search_by_entities(
        entities,
        "work",
        person_names=persons,
        settings=object(),  # type: ignore[arg-type]
        driver=drv,
        **kw,
    )


def _main_call(drv: FakeDriver) -> tuple[str, dict[str, Any]]:
    """取主查询那次调用（别名解析排在最前，要跳过）。"""
    for cypher, params in drv.session_obj.calls:
        if "ALIAS_OF" not in cypher:
            return cypher, params
    raise AssertionError("没有找到主查询调用")


# ---------------- 分流 ----------------

def test_persons_and_entities_passed_separately(driver: FakeDriver) -> None:
    """人/实体必须分开传进 Cypher —— 这是让动作扩展只对实体生效的前提。"""
    _search(driver, ["项目A"], ["张三"])
    _cypher, params = _main_call(driver)
    assert params["person_names"] == ["张三"]
    assert params["entity_names"] == ["项目A"]


def test_person_route_has_no_action(driver: FakeDriver) -> None:
    """路 1（人）里绝不能出现 ACTION —— 查人到直连就结束。"""
    person_leg = _SEARCH_CYPHER.split("UNION")[0]
    assert "INVOLVES" in person_leg
    assert "ACTION" not in person_leg, "查人这条路不该碰动作"


def test_entity_route_uses_action(driver: FakeDriver) -> None:
    """实体扩展必须用上 ACTION 边，否则这条边就是死代码。"""
    legs = _SEARCH_CYPHER.split("UNION")
    action_legs = [leg for leg in legs if "ACTION" in leg]
    assert action_legs, "至少有一条路要用 ACTION"
    assert any("MENTIONS" in leg for leg in action_legs), "动作扩展应从实体命中出发"


def test_entity_to_person_leg_exists(driver: FakeDriver) -> None:
    """实体 → 事件涉及的人 → 那些人的其他事件。"""
    legs = _SEARCH_CYPHER.split("UNION")
    hit = [leg for leg in legs if "insider" in leg and "peered" in leg]
    assert hit, "缺少「实体命中后带出人再扩展」这条路"
    assert "INVOLVES" in hit[0]


def test_entity_to_action_requires_shared_person(driver: FakeDriver) -> None:
    """同动作还不够，必须共享人 —— 否则所有「延期」事件都会涌进来。"""
    legs = _SEARCH_CYPHER.split("UNION")
    hit = [leg for leg in legs if "same:Event" in leg]
    assert hit, "缺少「同动作扩展」这条路"
    leg = hit[0]
    assert "ACTION" in leg
    assert "INVOLVES" in leg, "同动作的同时必须要求共享人，否则噪声太大"


def test_time_excluded_from_graph_channel(driver: FakeDriver) -> None:
    """时间过滤交给向量侧，图通道不该出现 Time。"""
    assert ":Time" not in _SEARCH_CYPHER


# ---------------- routed 诊断 ----------------

def test_routed_reports_person_and_entity_legs(driver: FakeDriver) -> None:
    result = _search(driver, ["项目A"], ["张三"])
    assert "person_direct" in result.routed
    assert "entity_direct" in result.routed
    assert "entity_to_action" in result.routed
    assert "entity_to_person" in result.routed


def test_routed_no_action_leg_when_only_persons(driver: FakeDriver) -> None:
    """只查人时不该报告动作扩展路。"""
    result = _search(driver, [], ["张三"])
    assert "entity_to_action" not in result.routed


# ---------------- 向后兼容 ----------------

def test_backward_compatible_without_person_names(driver: FakeDriver) -> None:
    """老调用方只传一个列表，仍要能跑（走同时覆盖人/实体的 Cypher）。"""
    result = search_by_entities(
        ["项目A", "张三"], "work", settings=object(), driver=driver  # type: ignore[arg-type]
    )
    assert result.ok is True
    assert result.event_ids == ["evt_a", "evt_b"]


def test_empty_names_short_circuits(driver: FakeDriver) -> None:
    result = _search(driver, [], [])
    assert result.event_ids == []
    assert driver.session_obj.calls == [], "空查询不该打 Neo4j"


def test_short_names_filtered(driver: FakeDriver) -> None:
    _search(driver, ["A"], ["张"])
    assert driver.session_obj.calls == [], "单字名字噪声太大，应被过滤"


# ---------------- 失败降级 ----------------

def test_driver_error_returns_degraded() -> None:
    class Boom:
        def session(self):
            raise RuntimeError("neo4j down")

    result = _search(Boom(), ["项目A"], ["张三"])  # type: ignore[arg-type]
    assert result.ok is False
    assert "neo4j down" in (result.error or "")
    assert result.event_ids == []


def test_driver_not_closed_when_injected(driver: FakeDriver) -> None:
    _search(driver, ["项目A"], ["张三"])
    # FakeDriver.close 什么都不做；这里断言的是「没有抛异常」
    assert driver.session_obj.calls
