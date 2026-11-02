"""B9 图 outbox：Event / Person / Time 三类节点 + 关系只追加不覆盖。

这是图层的核心契约测试。三类节点必须分得清，关系必须只追加。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.models.event_card import Entity, EventCard
from memory.services.graph_outbox import (
    APPEND_ENTITY_CYPHER,
    APPEND_PERSON_CYPHER,
    APPEND_TIME_CYPHER,
    LINK_CONTEXT_CYPHER,
    LINK_RELATED_CYPHER,
    MERGE_EVENT_CYPHER,
    OutboxResult,
    _split_entities,
    write_event_node,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _card(
    event_id: str = "evt_0123456789abcdef",
    *,
    summary: str = "张三和李四讨论了项目A",
    entities: list[Entity] | None = None,
    date_start: date = date(2026, 11, 2),
) -> EventCard:
    return EventCard(
        namespace="work",
        event_id=event_id,
        date_start=date_start,
        types=["general"],
        entities=entities
        if entities is not None
        else [
            Entity(name="张三", kind="person"),
            Entity(name="李四", kind="person"),
            Entity(name="项目A", kind="topic"),
        ],
        summary=summary,
        retention="normal",
        ingested_at=NOW,
    )


class FakeRecord:
    def __init__(self, value):
        self._value = value

    def __getitem__(self, idx):
        return self._value


class FakeResult:
    def __init__(self, single_value=None):
        self._single_value = single_value

    def consume(self):
        return self

    def single(self):
        return FakeRecord(self._single_value) if self._single_value is not None else None


class FakeSession:
    def __init__(self) -> None:
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher: str, **params):
        self.runs.append((cypher, params))
        if "CREATE (e)-[:RELATED" in cypher:
            return FakeResult(3)
        return FakeResult(None)


class FakeDriver:
    def __init__(self) -> None:
        self.session_obj = FakeSession()
        self.closed = False

    def session(self):
        return self.session_obj

    def close(self):
        self.closed = True


@pytest.fixture()
def driver() -> FakeDriver:
    return FakeDriver()


def _run(driver, card=None, **kw) -> OutboxResult:
    from memory.config import Settings

    return write_event_node(
        card or _card(),
        settings=Settings(),
        weaviate_id="uuid-1",
        driver=driver,
        **kw,
    )


# ---------------- 人从实体里分出来 ----------------


def test_split_entities_separates_person() -> None:
    persons, entities = _split_entities(_card())
    assert persons == ["张三", "李四"]
    assert [e["name"] for e in entities] == ["项目A"]
    assert entities[0]["kind"] == "topic"


def test_split_entities_all_persons() -> None:
    card = _card(entities=[Entity(name="张三", kind="person")])
    persons, entities = _split_entities(card)
    assert persons == ["张三"]
    assert entities == []


def test_split_entities_no_persons() -> None:
    card = _card(entities=[Entity(name="项目A", kind="topic")])
    persons, entities = _split_entities(card)
    assert persons == []
    assert entities == [{"name": "项目A", "kind": "topic"}]


# ---------------- 三类节点都建出来 ----------------


def test_person_node_created(driver: FakeDriver) -> None:
    _run(driver)
    runs = [p for c, p in driver.session_obj.runs if c == APPEND_PERSON_CYPHER]
    assert len(runs) == 1
    assert runs[0]["persons"] == ["张三", "李四"]


def test_entity_node_excludes_persons(driver: FakeDriver) -> None:
    """人不能同时出现在 Entity 里 —— 否则两张图各存一份，对不上。"""
    _run(driver)
    runs = [p for c, p in driver.session_obj.runs if c == APPEND_ENTITY_CYPHER]
    assert len(runs) == 1
    names = [e["name"] for e in runs[0]["entities"]]
    assert names == ["项目A"]
    assert "张三" not in names


def test_time_node_created(driver: FakeDriver) -> None:
    _run(driver)
    runs = [p for c, p in driver.session_obj.runs if c == APPEND_TIME_CYPHER]
    assert len(runs) == 1
    assert runs[0]["time_key"] == "2026-11-02"
    assert runs[0]["time_year"] == 2026
    assert runs[0]["time_month"] == "2026-11"


def test_time_node_uses_date_start(driver: FakeDriver) -> None:
    """时间节点跟事件发生日，不是写入日。"""
    card = _card(date_start=date(2025, 3, 15))
    _run(driver, card)
    runs = [p for c, p in driver.session_obj.runs if c == APPEND_TIME_CYPHER]
    assert runs[0]["time_key"] == "2025-03-15"


def test_result_reports_counts(driver: FakeDriver) -> None:
    result = _run(driver, context="A股")
    assert result.ok is True
    assert result.persons == 2
    assert result.entities == 1
    assert result.time_key == "2026-11-02"


# ---------------- 关系只追加不覆盖 ----------------


def test_person_edge_is_created_not_merged(driver: FakeDriver) -> None:
    _run(driver)
    assert "CREATE (e)-[:INVOLVES" in APPEND_PERSON_CYPHER
    assert "MERGE (e)-[:INVOLVES" not in APPEND_PERSON_CYPHER


def test_entity_edge_is_created_not_merged(driver: FakeDriver) -> None:
    _run(driver)
    assert "CREATE (e)-[:MENTIONS" in APPEND_ENTITY_CYPHER
    assert "MERGE (e)-[:MENTIONS" not in APPEND_ENTITY_CYPHER


def test_time_edge_is_created_not_merged(driver: FakeDriver) -> None:
    _run(driver)
    assert "CREATE (e)-[:AT" in APPEND_TIME_CYPHER
    assert "MERGE (e)-[:AT" not in APPEND_TIME_CYPHER


def test_related_links_are_created_not_merged(driver: FakeDriver) -> None:
    """**核心不变量**：event-event 关系只追加，绝不覆盖。

    同一条记忆被反复提起是最有价值的信号。用 MERGE + SET 会抹掉
    上一次的时间，等于把这个信号删了。
    """
    _run(driver)
    assert "CREATE (e)-[:RELATED" in LINK_RELATED_CYPHER
    assert "MERGE (e)-[:RELATED" not in LINK_RELATED_CYPHER
    assert "SET r." not in LINK_RELATED_CYPHER, "对关系做 SET 就是覆盖"


def test_context_links_are_created_not_merged(driver: FakeDriver) -> None:
    _run(driver, context="A股")
    assert "CREATE (c)-[:RELATES_TO" in LINK_CONTEXT_CYPHER
    assert "MERGE (c)-[:RELATES_TO" not in LINK_CONTEXT_CYPHER


def test_all_relations_carry_timestamp(driver: FakeDriver) -> None:
    """每条边都要能回答「什么时候建立的」。"""
    _run(driver, context="A股")
    relation_runs = [
        (c, p)
        for c, p in driver.session_obj.runs
        if c
        in (
            APPEND_PERSON_CYPHER,
            APPEND_ENTITY_CYPHER,
            APPEND_TIME_CYPHER,
            LINK_RELATED_CYPHER,
            LINK_CONTEXT_CYPHER,
        )
    ]
    assert len(relation_runs) == 5
    for cypher, params in relation_runs:
        assert "created_at" in params, f"{cypher.strip()[:30]} 缺 created_at"
        assert params["created_at"] == NOW.isoformat()


# ---------------- 执行顺序 ----------------


def test_event_node_first(driver: FakeDriver) -> None:
    _run(driver)
    assert driver.session_obj.runs[0][0] == MERGE_EVENT_CYPHER


def test_nodes_before_related(driver: FakeDriver) -> None:
    """必须先建 Person/Entity 边，`LINK_RELATED` 靠它找共享节点。"""
    _run(driver)
    order = [c for c, _ in driver.session_obj.runs]
    assert order.index(APPEND_PERSON_CYPHER) < order.index(LINK_RELATED_CYPHER)
    assert order.index(APPEND_ENTITY_CYPHER) < order.index(LINK_RELATED_CYPHER)


def test_related_matches_both_person_and_entity(driver: FakeDriver) -> None:
    """共享人 或 共享实体 都算有关系。"""
    _run(driver)
    assert "[:MENTIONS|INVOLVES]" in LINK_RELATED_CYPHER


# ---------------- 可选性 ----------------


def test_no_entities_still_writes_event_and_time(driver: FakeDriver) -> None:
    card = _card(entities=[])
    result = _run(driver, card)
    assert result.ok is True
    assert result.persons == 0
    assert result.entities == 0
    cyphers = [c for c, _ in driver.session_obj.runs]
    assert MERGE_EVENT_CYPHER in cyphers
    assert APPEND_TIME_CYPHER in cyphers, "时间节点和实体无关，仍要建"
    assert APPEND_PERSON_CYPHER not in cyphers
    assert LINK_RELATED_CYPHER not in cyphers


def test_only_persons_no_entity_run(driver: FakeDriver) -> None:
    card = _card(entities=[Entity(name="张三", kind="person")])
    _run(driver, card)
    cyphers = [c for c, _ in driver.session_obj.runs]
    assert APPEND_PERSON_CYPHER in cyphers
    assert APPEND_ENTITY_CYPHER not in cyphers


def test_no_context_skips_context_edge(driver: FakeDriver) -> None:
    _run(driver)
    cyphers = [c for c, _ in driver.session_obj.runs]
    assert LINK_CONTEXT_CYPHER not in cyphers


def test_context_edge_created_when_given(driver: FakeDriver) -> None:
    result = _run(driver, context="A股")
    assert result.ok is True
    ctx_runs = [p for c, p in driver.session_obj.runs if c == LINK_CONTEXT_CYPHER]
    assert ctx_runs[0]["context"] == "A股"


def test_related_link_count_returned(driver: FakeDriver) -> None:
    assert _run(driver).related_links == 3


# ---------------- 失败绝不阻断 ----------------


def test_driver_error_returns_not_ok_never_raises() -> None:
    class BoomDriver:
        def session(self):
            raise RuntimeError("neo4j 挂了")

        def close(self):
            pass

    result = _run(BoomDriver())
    assert result.ok is False
    assert "neo4j 挂了" in (result.error or "")


def test_injected_driver_is_not_closed(driver: FakeDriver) -> None:
    _run(driver)
    assert driver.closed is False
