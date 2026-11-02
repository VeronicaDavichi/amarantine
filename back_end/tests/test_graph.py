"""B9 图 outbox：关系只追加不覆盖 + Event 节点幂等。

这是图层的核心契约测试。之前图模块没有任何单测覆盖，
所以「关系被 MERGE 覆盖」这类问题不会被发现。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.models.event_card import Entity, EventCard
from memory.services.graph_outbox import (
    APPEND_MENTION_CYPHER,
    LINK_CONTEXT_CYPHER,
    LINK_RELATED_CYPHER,
    MERGE_EVENT_CYPHER,
    OutboxResult,
    write_event_node,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _card(
    event_id: str = "evt_0123456789abcdef",
    *,
    summary: str = "张三决定项目A延期到六月",
    entities: list[Entity] | None = None,
) -> EventCard:
    return EventCard(
        namespace="work",
        event_id=event_id,
        date_start=date(2026, 6, 1),
        types=["decision"],
        entities=entities
        if entities is not None
        else [Entity(name="张三", kind="person"), Entity(name="项目A", kind="topic")],
        summary=summary,
        retention="normal",
        ingested_at=NOW,
    )


class FakeRecord:
    """假装是 neo4j 的 Record：支持按索引取值。"""

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
    """记录执行了哪些 Cypher。"""

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


# ---------------- 节点幂等 vs 关系追加 ----------------


def test_event_node_is_merged(driver: FakeDriver) -> None:
    _run(driver)
    assert driver.session_obj.runs[0][0] == MERGE_EVENT_CYPHER


def test_entity_mentions_are_created_not_merged(driver: FakeDriver) -> None:
    """实体边必须 `CREATE` —— `MERGE` 会抹掉上一次的提及时间。"""
    _run(driver)
    cypher = [c for c, _ in driver.session_obj.runs if c == APPEND_MENTION_CYPHER]
    assert len(cypher) == 1
    assert "CREATE (e)-[:MENTIONS" in APPEND_MENTION_CYPHER
    assert "MERGE (e)-[:MENTIONS" not in APPEND_MENTION_CYPHER


def test_related_links_are_created_not_merged(driver: FakeDriver) -> None:
    """**核心不变量**：event-event 关系只追加，绝不覆盖。

    同一条记忆被反复提起是最有价值的信号。用 MERGE + SET 会抹掉
    上一次的时间，等于把这个信号删了。
    """
    _run(driver)
    cypher = [c for c, _ in driver.session_obj.runs if c == LINK_RELATED_CYPHER]
    assert len(cypher) == 1
    assert "CREATE (e)-[:RELATED" in LINK_RELATED_CYPHER
    assert "MERGE (e)-[:RELATED" not in LINK_RELATED_CYPHER
    assert "SET r." not in LINK_RELATED_CYPHER, "对关系做 SET 就是覆盖"


def test_context_links_are_created_not_merged(driver: FakeDriver) -> None:
    _run(driver, context="A股")
    cypher = [c for c, _ in driver.session_obj.runs if c == LINK_CONTEXT_CYPHER]
    assert len(cypher) == 1
    assert "CREATE (c)-[:RELATES_TO" in LINK_CONTEXT_CYPHER
    assert "MERGE (c)-[:RELATES_TO" not in LINK_CONTEXT_CYPHER


def test_all_relations_carry_timestamp(driver: FakeDriver) -> None:
    """每条边都要能回答「什么时候建立的」。"""
    _run(driver, context="A股")
    relation_runs = [
        (c, p)
        for c, p in driver.session_obj.runs
        if c in (APPEND_MENTION_CYPHER, LINK_RELATED_CYPHER, LINK_CONTEXT_CYPHER)
    ]
    assert len(relation_runs) == 3
    for cypher, params in relation_runs:
        assert "created_at" in params, f"{cypher.strip()[:30]} 缺 created_at"
        assert params["created_at"] == NOW.isoformat()


# ---------------- 执行顺序 ----------------


def test_mentions_before_related(driver: FakeDriver) -> None:
    """必须先建 `MENTIONS` 边，`LINK_RELATED` 靠它找共享实体。"""
    _run(driver)
    order = [c for c, _ in driver.session_obj.runs]
    assert order.index(APPEND_MENTION_CYPHER) < order.index(LINK_RELATED_CYPHER)


def test_event_node_first(driver: FakeDriver) -> None:
    _run(driver)
    assert driver.session_obj.runs[0][0] == MERGE_EVENT_CYPHER


# ---------------- 可选性 ----------------


def test_no_entities_skips_relations(driver: FakeDriver) -> None:
    """没有实体就没有边可建，但节点仍然要写。"""
    card = _card(entities=[])
    result = _run(driver, card)
    assert result.ok is True
    assert result.related_links == 0
    cyphers = [c for c, _ in driver.session_obj.runs]
    assert MERGE_EVENT_CYPHER in cyphers
    assert APPEND_MENTION_CYPHER not in cyphers
    assert LINK_RELATED_CYPHER not in cyphers


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
    result = _run(driver)
    assert result.related_links == 3


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
    """外部传进来的 driver 由调用方管，我们不能替它关。"""
    _run(driver)
    assert driver.closed is False
