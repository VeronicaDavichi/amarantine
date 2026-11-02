"""C1 Neo4j 实体扩展通道。

ADR 的超时矩阵里，图通道是 **200ms 且失败直接丢弃该路**：

- 图可丢弃重建（D2），所以它天然是可降级的
- 但它不能拖慢整体：`degraded=true` 只是标记某路缺失，不影响返回

## 人跟实体走不同的路（这是本通道的核心）

**查人**：`(e)-[:INVOLVES]->(:Person {name})` 直接命中就收工，**不碰动作**。

人是稀有枢纽 —— 「张三」这个名字出现在查询里，本身就是极强的信号。
再去查他做过什么动作只会引入噪声：`张三批评了项目A` 和
`张三决定项目B延期` 都会被拉进来，而查询可能只想要「张三相关的事」。

**查实体**：命中后继续沿着事件**挖出动作和相关人**，再扩一层。

实体（项目A / 腾讯公司）往往被提过很多次，光看「提过项目A 的事件」
只能召回字面重复的那些。真正有价值的是**谁**对项目A 做过**什么** ——
所以 Entity 命中后要经 `ACTION` / `INVOLVES` 走到人和动作上，
再由这些人和动作扩展到本该相关、但没提到项目A 的事件。

## 查询走六条路（UNION）

1. **Person 直连**（准，终点）：查人到此为止
2. **Entity 直连**（准）：字面提到该实体的事件
3. **Entity → 事件的人 → 那些人的其他事件**（广）：`项目A` → `张三` → 张三的其他事
4. **Entity → 动作 → 同动作且共享人的事件**（广）：`项目A被延期` → 其他"某人延期某物"
5. **`RELATED` 一跳扩展**（广）：顺共享节点连到相邻事件
6. **`summary CONTAINS`**（兜底）：加边之前的旧数据没有边，只能靠文本

路 4 要求**同动作且共享人**两个条件同时成立。只按动作匹配会让所有
"延期"事件涌进来（`延期` 是个高频动词，跟查询意图未必相关）。

`Time` 不参与这个通道 —— 时间过滤是结构化条件（日期区间），
放在向量侧按 `date_start` 过滤更合适，走图反而绕远。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from memory.config import Settings

logger = logging.getLogger("memory.graph_search")

#: 图通道超时（ADR D3）
GRAPH_TIMEOUT_S = 0.2

#: 一次查询最多用几个实体名，避免 Cypher 参数爆炸
MAX_ENTITY_NAMES = 8

#: 实体名最短长度——单字实体噪声太大
MIN_ENTITY_LEN = 2

#: 六条路合并后的上限
DEFAULT_LIMIT = 50

#: 所有名字（人 + 实体），用于 RELATED 扩展和文本兜底
_ALL_NAMES_CYPHER = """
MATCH (e:Event {namespace: $namespace})-[:INVOLVES]->(p:Person)
WHERE p.name IN $names
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit

UNION

MATCH (e:Event {namespace: $namespace})-[:MENTIONS]->(n:Entity)
WHERE n.name IN $names
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit

UNION

MATCH (e:Event {namespace: $namespace})-[:MENTIONS|INVOLVES]->(shared)
WHERE shared.name IN $names
MATCH (e)-[:RELATED]-(other:Event {namespace: $namespace})
WITH DISTINCT other
RETURN other.event_id AS event_id
LIMIT $limit

UNION

MATCH (e:Event {namespace: $namespace})
WHERE ANY(name IN $names WHERE e.summary CONTAINS name)
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit
"""

#: 人跟实体分流的版本。
#:
#: `$person_names` 只走路 1，**绝不进动作扩展**；
#: `$entity_names` 走「命中 → 挖动作/人 → 再扩一层」。
_SEARCH_CYPHER = """
// 路 1：人直连 —— 终点，不碰动作
MATCH (e:Event {namespace: $namespace})-[:INVOLVES]->(p:Person)
WHERE p.name IN $person_names
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit

UNION

// 路 2：实体直连 —— 字面提到它的事件
MATCH (e:Event {namespace: $namespace})-[:MENTIONS]->(n:Entity)
WHERE n.name IN $entity_names
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit

UNION

// 路 3：实体 → 这条事件涉及的人 → 那些人的其他事件
MATCH (e:Event {namespace: $namespace})-[:MENTIONS]->(n:Entity)
WHERE n.name IN $entity_names
MATCH (e)-[:INVOLVES]->(insider:Person)
MATCH (insider)<-[:INVOLVES]-(peered:Event {namespace: $namespace})
WITH DISTINCT peered
RETURN peered.event_id AS event_id
LIMIT $limit

UNION

// 路 4：实体 → 这条事件的动作 → 同动作且共享人的其他事件
MATCH (e:Event {namespace: $namespace})-[:MENTIONS]->(n:Entity)
WHERE n.name IN $entity_names
MATCH (e)-[:ACTION]->(a:Action)
MATCH (a)<-[:ACTION]-(same:Event {namespace: $namespace})
WHERE same.event_id <> e.event_id
MATCH (same)-[:INVOLVES]->(p:Person)
MATCH (e)-[:INVOLVES]->(p)
WITH DISTINCT same
RETURN same.event_id AS event_id
LIMIT $limit

UNION

// 路 5：顺共享人/实体的一跳邻居（广）
MATCH (e:Event {namespace: $namespace})-[:MENTIONS|INVOLVES]->(shared)
WHERE shared.name IN $names
MATCH (e)-[:RELATED]-(other:Event {namespace: $namespace})
WITH DISTINCT other
RETURN other.event_id AS event_id
LIMIT $limit

UNION

// 路 6：兜底——加边之前的旧数据只能靠文本匹配
MATCH (e:Event {namespace: $namespace})
WHERE ANY(name IN $names WHERE e.summary CONTAINS name)
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit
"""


@dataclass(frozen=True)
class GraphHits:
    """图通道结果。`ok=False` 时上游应置 `degraded=true` 并继续。"""

    event_ids: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    #: 本次走了哪几条路（诊断用，便于确认动作扩展真的生效）
    routed: tuple[str, ...] = ()


def _clean_names(entity_names: list[str]) -> list[str]:
    """过滤过长/过短/重复的实体名，保持顺序。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in entity_names:
        text = str(name).strip()
        if len(text) < MIN_ENTITY_LEN or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= MAX_ENTITY_NAMES:
            break
    return cleaned


def search_by_entities(
    entity_names: list[str],
    namespace: str,
    *,
    person_names: list[str] | None = None,
    settings: Settings,
    limit: int = 50,
    timeout_s: float = GRAPH_TIMEOUT_S,
    driver: Any | None = None,
) -> GraphHits:
    """按实体名扩展候选 event_id。

    人跟实体走不同的路（见模块文档）：

    - `person_names` 里的名字只做直连匹配，**不触发动作扩展**
    - `entity_names` 里的名字命中后，会经事件的人/动作再扩一层

    Args:
        entity_names: 查询里抽到的**非人**实体名（项目/机构/地点）。
        namespace: 记忆域（Cypher 里强制参与匹配）。
        person_names: 查询里抽到的**人**名。不传则退回老行为
            （所有名字一视同仁，不走动作扩展）。
        settings: Neo4j 连接配置。
        limit: 最多返回条数。
        timeout_s: 超时秒数（ADR 200ms）。
        driver: 可注入的 driver（测试用）。

    Returns:
        GraphHits；**不抛异常**，失败返回 ``ok=False``。
    """
    persons = _clean_names(person_names or [])
    entities = _clean_names(entity_names)
    # 老调用方只传一个列表：全部当实体处理（不退化成纯 RELATED，仍比之前强）
    if person_names is None:
        entities = _clean_names(list(entity_names))
    all_names = _clean_names(persons + entities)
    if not all_names:
        return GraphHits(event_ids=[], ok=True)

    owns_driver = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
                connection_timeout=timeout_s,
            )

        # 有人名就用分流版（动作扩展只对实体生效）；
        # 没人名说明调用方没分过流，用一条同时覆盖人/实体的老 Cypher。
        #
        # 判据是 `person_names is not None` 而不是 `bool(persons)`：
        # 调用方可能明确传空列表（「这次查询没抽出人」），这是分流版的有效输入，
        # 不能因为列表空就退回老路径 —— 那样实体扩展也会一起失效。
        use_split = person_names is not None
        cypher = _SEARCH_CYPHER if use_split else _ALL_NAMES_CYPHER
        params = {
            "namespace": namespace,
            "names": all_names,
            "person_names": persons,
            "entity_names": entities,
            "limit": int(limit),
            "timeout": timeout_s,
        }

        with drv.session() as session:
            result = session.run(cypher, **params)
            event_ids = [row["event_id"] for row in result if row.get("event_id")]

        routed = ["person_direct", "entity_direct"] if use_split else ["direct"]
        if use_split and entities:
            routed.extend(["entity_to_person", "entity_to_action"])
        routed.extend(["related", "text_fallback"])
        return GraphHits(event_ids=event_ids, ok=True, routed=tuple(routed))
    except Exception as exc:  # noqa: BLE001 - 图失败必须降级，不能抛给上层
        message = f"图扩展失败（已降级）: {exc}"
        logger.warning("%s namespace=%s", message, namespace)
        return GraphHits(event_ids=[], ok=False, error=str(exc))
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)
