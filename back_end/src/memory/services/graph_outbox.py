"""B9 图 outbox：幂等写 Event 节点，**关系只追加不覆盖**。

ADR D2 的红线是「**图可丢弃重建**」：

- 写入顺序：先 Weaviate 成功 → 再发图更新
- 图写失败**只告警，不回滚、不阻断**
- 节点/边统一带 `namespace`，禁止跨域

## 为什么关系只追加

事件卡本身幂等（`event_id` 是内容哈希），所以 `Event` 节点用 `MERGE`
不会出问题。但**关系不能 MERGE**：

`MERGE (a)-[r:RELATED]->(b) SET r.at = $now` 会抹掉上一次的时间。
同一条记忆被反复提起，恰恰是最有价值的信号 —— 覆盖掉历史等于把这个信号删了。

所以关系一律 `CREATE`：同一对节点之间可以有多条边，各自带 `created_at`，
时间线完整保留。（Neo4j 不做忘却处理：删卡时图上的边留在原地。）

## 图结构

```
(:Event {namespace, event_id, ...})
(:Entity {namespace, name})
(:Context {namespace, key})

(Event)-[:MENTIONS {created_at}]->(Entity)
(Event)-[:RELATED  {kind, created_at}]->(Event)
(Context)-[:RELATES_TO {created_at}]->(Event)
```

**`RELATED` 的产生条件**：两个 Event 共享至少一个 Entity。
这就是「不同 event 之间必有关系」的落法 —— 有关系才有边，没交集不硬连。

Cypher 里关系不能作为边的起点，所以「把 Context 连到某条关系上」
用 `(Context)-[:RELATES_TO]->(Event)` 表达：事件是关系的锚点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from memory.config import Settings
from memory.models.event_card import EventCard

logger = logging.getLogger("memory.graph")

#: 一次写入最多连出多少条 event-event 边（防边数爆炸）
MAX_RELATED_LINKS = 20

#: Event 节点：`MERGE` 是安全的，因为 event_id 是内容哈希，同一条内容不会有两份
MERGE_EVENT_CYPHER = """
MERGE (e:Event {namespace: $namespace, event_id: $event_id})
SET e.summary     = $summary,
    e.date_start  = $date_start,
    e.date_end    = $date_end,
    e.types       = $types,
    e.importance  = $importance,
    e.pinned      = $pinned,
    e.retention   = $retention,
    e.weaviate_id = $weaviate_id,
    e.ingested_at = $ingested_at
RETURN e
"""

#: 实体边：每次提及都留一条，`created_at` 各不相同
APPEND_MENTION_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
UNWIND $names AS name
MERGE (n:Entity {namespace: $namespace, name: name})
  ON CREATE SET n.kind = $kind
CREATE (e)-[:MENTIONS {created_at: $created_at}]->(n)
RETURN count(*)
"""

#: event-event 边：共享实体的两个 Event 连一条，**只追加**
LINK_RELATED_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MATCH (e)-[:MENTIONS]->(:Entity)<-[:MENTIONS]-(other:Event {namespace: $namespace})
WHERE other.event_id <> e.event_id
WITH DISTINCT e, other
LIMIT $max_links
CREATE (e)-[:RELATED {kind: 'shared_entity', created_at: $created_at}]->(other)
RETURN count(*)
"""

#: 上下文边：把当前话题挂到这条事件上
LINK_CONTEXT_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MERGE (c:Context {namespace: $namespace, key: $context})
CREATE (c)-[:RELATES_TO {created_at: $created_at}]->(e)
RETURN c
"""


@dataclass(frozen=True)
class OutboxResult:
    """图更新的结果。`ok=False` 时链路继续走，只是留一条告警。"""

    ok: bool
    error: str | None = None
    #: 本次新建的 event-event 边数（诊断用）
    related_links: int = 0


def write_event_node(
    card: EventCard,
    *,
    settings: Settings,
    weaviate_id: str | None,
    context: str | None = None,
    driver: object | None = None,
) -> OutboxResult:
    """把事件卡同步成图节点，并**追加**这次提及产生的关系。

    四步顺序不能换：

    1. `MERGE` Event 节点 —— 节点幂等
    2. `CREATE` 实体边 —— 必须先有 `MENTIONS` 才能算共享实体
    3. `CREATE` event-event 边 —— 依赖第 2 步建好的边
    4. `CREATE` context 边（可选）

    Args:
        card: 事件卡。
        settings: Neo4j 连接配置。
        weaviate_id: 回指 Weaviate 对象的 UUID（A8 要求双向可解析）。
        context: 当前对话话题（如 ``"A股"``）。给了才建 Context 边。
        driver: 可注入的 neo4j driver（测试用）。

    Returns:
        OutboxResult；**不会抛异常**，失败仅告警（ADR D2）。
    """
    owns_driver = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )

        params = {
            "namespace": card.namespace,
            "event_id": card.event_id,
            "summary": card.summary,
            "date_start": card.date_start.isoformat(),
            "date_end": card.date_end.isoformat() if card.date_end else None,
            "types": list(card.types),
            "importance": card.importance,
            "pinned": card.pinned,
            "retention": card.retention,
            "weaviate_id": weaviate_id,
            "ingested_at": card.ingested_at.isoformat(),
            # 关系的统一时间戳：这次「提及」发生的时刻
            "created_at": card.ingested_at.isoformat(),
        }
        names = [e.name for e in card.entities]
        kind = card.entities[0].kind if card.entities else "topic"

        with drv.session() as session:
            session.run(MERGE_EVENT_CYPHER, **params).consume()

            if names:
                session.run(
                    APPEND_MENTION_CYPHER,
                    **params,
                    names=names,
                    kind=kind,
                ).consume()

                record = session.run(
                    LINK_RELATED_CYPHER,
                    **params,
                    max_links=MAX_RELATED_LINKS,
                ).single()
                related = int(record[0]) if record and record[0] is not None else 0
            else:
                related = 0

            if context:
                session.run(LINK_CONTEXT_CYPHER, **params, context=context).consume()

        return OutboxResult(ok=True, related_links=related)
    except Exception as exc:  # noqa: BLE001 - 图失败绝不阻断主链路
        message = f"图写入失败（已忽略，等待重建对账）: {exc}"
        logger.warning("%s event_id=%s", message, card.event_id)
        return OutboxResult(ok=False, error=str(exc))
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001 - 关闭失败也不该影响主流程
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)
