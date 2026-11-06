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
from memory.services.graph_outbox import RESOLVE_ALIAS_CYPHER

logger = logging.getLogger("memory.graph_search")

#: 图通道超时（ADR D3）
# 默认超时不再硬编码：`GRAPH_TIMEOUT_MS` 才是唯一真值来源（见函数体内）。

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

UNION

// 别名主人直连（同分流版的第 7 条路）。老调用方也要能吃到别名。
MATCH (e:Event {namespace: $namespace})-[:INVOLVES|MENTIONS]->(host)
WHERE host.name IN $alias_hosts
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit
"""

#: 人跟实体分流的版本。
#:
#: `$person_names` 只走路 1，**绝不进动作扩展**；
#: `$entity_names` 走「命中 → 挖动作/人 → 再扩一层」；
#: `$alias_hosts` 走路 7，**只直连、不扩展**。
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

UNION

// 路 7：别名主人直连 —— 人还是物都走
//
// 别名解析出来的主人（`糖包` → `豆包`）可能只有 `:Entity` 标签：
// 别名声明不说明那个东西是什么，实体抽取也可能还没把它判定成人。
// 路 1 要求终点是 `:Person`、路 2 要求 `:Entity` 且查 `$entity_names`，
// 两条都进不去 —— 别名解析出来了，图通道却接不上。
//
// 所以单独一条：**终点不限定标签**，`INVOLVES` 和 `MENTIONS` 都认。
//
// **只直连，不扩展。** 别名大多是 uncertain，拿弱证据去挖动作/同僚
// 会把噪声指数放大；而且别名声明本来就不构成「提到」——
// 它只说明「这两个名字指同一个东西」，不说明这句话跟那个东西有关。
MATCH (e:Event {namespace: $namespace})-[:INVOLVES|MENTIONS]->(host)
WHERE host.name IN $alias_hosts
WITH DISTINCT e
RETURN e.event_id AS event_id
LIMIT $limit
"""


@dataclass(frozen=True)
class AliasHit:
    """一条被解析的别名。"""

    #: 查询里说的那个称呼（`小张`）
    alias: str
    #: 它指向的主人（`张三`）
    canonical: str
    #: 置信度等级：exact / probable / uncertain
    label: str
    #: 数值置信度
    confidence: float


@dataclass(frozen=True)
class GraphHits:
    """图通道结果。`ok=False` 时上游应置 `degraded=true` 并继续。"""

    event_ids: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    #: 本次走了哪几条路（诊断用，便于确认动作扩展真的生效）
    routed: tuple[str, ...] = ()
    #: 本次解析到的别名。**低置信度的会出现在这里，让上层自己决定要不要提示用户。**
    alias_hits: tuple[AliasHit, ...] = ()

    @property
    def ambiguous_aliases(self) -> tuple[str, ...]:
        """一个称呼对应多个主人时，把那些称呼列出来。

        `三哥` 既能指印度人，也能指家里排行第三的男性 —— 这种歧义
        不能替用户拍板，只能如实报出来。
        """
        by_alias: dict[str, set[str]] = {}
        for hit in self.alias_hits:
            by_alias.setdefault(hit.alias, set()).add(hit.canonical)
        return tuple(sorted(a for a, owners in by_alias.items() if len(owners) > 1))

    @property
    def uncertain_aliases(self) -> tuple[str, ...]:
        """置信度低于 probable 的别名（召回结果需要对用户标注）。"""
        return tuple(
            sorted({h.alias for h in self.alias_hits if h.label == "uncertain"})
        )


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
    timeout_s: float | None = None,
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
    # `GRAPH_TIMEOUT_MS` 是唯一真值来源；显式传参仍可覆盖（测试用）。
    effective_timeout_s = (
        timeout_s
        if timeout_s is not None
        else settings.GRAPH_TIMEOUT_MS / 1000.0
    )

    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
                connection_timeout=effective_timeout_s,
            )

        # 有人名就用分流版（动作扩展只对实体生效）；
        # 没人名说明调用方没分过流，用一条同时覆盖人/实体的老 Cypher。
        #
        # 判据是 `person_names is not None` 而不是 `bool(persons)`：
        # 调用方可能明确传空列表（「这次查询没抽出人」），这是分流版的有效输入，
        # 不能因为列表空就退回老路径 —— 那样实体扩展也会一起失效。
        use_split = person_names is not None

        # 先解析别名：`小张` → `张三`。
        #
        # 一个人可能有多种称呼，直接把 `小张` 拿去匹配 `(:Person {name})`
        # 会一条都命中不了。别名不是独立的实体，它得先落到主人身上。
        #
        # 单独一次查询而不是塞进上面的大 UNION：UNION 要求每路列数一致，
        # 别名路还要额外带回置信度，混在一起会把所有路的返回结构撑大。
        alias_hits: list[AliasHit] = []
        #: 别名解析出的主人，专供路 7。跟 `persons` / `entities` 都不混。
        alias_hosts: list[str] = []
        with drv.session() as session:
            if all_names:
                for row in session.run(
                    RESOLVE_ALIAS_CYPHER, namespace=namespace, names=all_names
                ):
                    alias_hits.append(
                        AliasHit(
                            alias=row["alias"],
                            canonical=row["canonical"],
                            label=row["label"] or "uncertain",
                            confidence=float(row["confidence"] or 0.4),
                        )
                    )
                # 别名的主人**不进 `persons`**，走独立的路 7。
                #
                # 塞进 persons 会让路 1 去匹配 `(p:Person)`，而别名宿主
                # 常常只有 `:Entity` 标签（别名声明不说明那东西是什么），
                # 路 1 直接落空 —— 别名解析出来了，图通道却接不上。
                #
                # 塞进 entity_names 更糟：那会触发路 3（挖同僚人）和
                # 路 4（挖同动作），拿 uncertain 的弱证据去扩散。
                #
                # 一个别名可能解析出多个主人（`三哥` 的歧义），全部带上 ——
                # 宁可多召回，也不能替用户挑一个。
                resolved = _clean_names([h.canonical for h in alias_hits])
                if resolved:
                    alias_hosts = list(resolved)
                    all_names = _clean_names(all_names + resolved)

            cypher = _SEARCH_CYPHER if use_split else _ALL_NAMES_CYPHER
            params = {
                "namespace": namespace,
                "names": all_names,
                "person_names": persons,
                "entity_names": entities,
                "alias_hosts": alias_hosts,
                "limit": int(limit),
                "timeout": effective_timeout_s,
            }
            result = session.run(cypher, **params)
            event_ids = [row["event_id"] for row in result if row.get("event_id")]

        routed = ["person_direct", "entity_direct"] if use_split else ["direct"]
        if use_split and entities:
            routed.extend(["entity_to_person", "entity_to_action"])
        if alias_hits:
            routed.insert(0, "alias_resolved")
        routed.extend(["related", "text_fallback"])
        return GraphHits(
            event_ids=event_ids,
            ok=True,
            routed=tuple(routed),
            alias_hits=tuple(alias_hits),
        )
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


# ---------------------------------------------------------------- 孤岛判定（spec 022）

#: 事件级邻域：通过共享 `MENTIONS`/`INVOLVES` 实体能到达的**其它 Event** 数量。
#:
#: **不能用「邻居平均度数」** —— 实测过的反例：
#: 张三事件的邻居是 `[Time(3), Person(1), Entity(1), Entity(1), Action(1)]`，
#: 平均 1.40；而它其实是最核心的记忆。原因是 Time/Entity/Action 天然只连
#: 一个事件，它们的度数反映的是「实体被提到几次」，不是「事件孤不孤立」。
#:
#: 正确的问法是：从这个事件出发，能不能走到**别的事件**。
_NEIGHBOR_CYPHER = """
UNWIND $event_ids AS eid
MATCH (e:Event {namespace: $namespace, event_id: eid})
OPTIONAL MATCH (e)-[:MENTIONS|INVOLVES]->(shared)<-[:MENTIONS|INVOLVES]-(other:Event)
WHERE other IS NULL OR other <> e
RETURN eid AS event_id, count(DISTINCT other) AS neighbor_events
"""

#: namespace 里的事件总数（数据量门槛用）。
_TOTAL_EVENTS_CYPHER = """
MATCH (e:Event {namespace: $namespace})
RETURN count(e) AS total
"""


@dataclass(frozen=True)
class EventNeighborhood:
    """一次查询拿到「每个事件的邻域」+「namespace 总量」。

    合在一起返回是为了少一次往返 —— 数据量门槛和邻域数是同时需要的。
    """

    #: event_id -> 能到达的其它 Event 数
    neighbors: dict[str, int]
    #: namespace 里 Event 节点总数（判断数据够不够支撑孤岛判定）
    total_events: int


def event_neighborhood(
    event_ids: list[str],
    namespace: str,
    *,
    settings: Any,
    driver: Any | None = None,
    timeout_s: float = 3.0,
) -> EventNeighborhood | None:
    """批量查事件级邻域，供孤岛判定使用（spec 022）。

    **返回值有两种含义，不要混淆**：

    - `EventNeighborhood`：正常
    - `None`：**图不可用**。调用方必须**不判孤岛**（`island=False`）并标注
      `graph_degraded=true`。
      不能把「查不了」当成「没有邻居」—— 那会在图抖动时把全库当成孤岛，
      一次性加速淘汰所有记忆。与 `event_degrees` 的 `None` 语义一致。

    Args:
        event_ids: 要查的 event_id 列表。
        namespace: 记忆域。
        settings: 提供 Neo4j 连接参数。
        driver: 可注入的 driver（测试用）。
        timeout_s: 连接超时。

    Returns:
        `EventNeighborhood`，或 `None` 表示图不可用。
    """
    if not event_ids:
        return EventNeighborhood(neighbors={}, total_events=0)

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
        with drv.session() as session:
            records = session.run(
                _NEIGHBOR_CYPHER,
                event_ids=list(event_ids),
                namespace=namespace,
            )
            neighbors = {
                r["event_id"]: int(r["neighbor_events"] or 0) for r in records
            }
            total = session.run(
                _TOTAL_EVENTS_CYPHER, namespace=namespace
            ).single()
            total_events = int(total["total"]) if total else 0
            return EventNeighborhood(
                neighbors=neighbors, total_events=total_events
            )
    except Exception as exc:  # noqa: BLE001 - 图不可用必须让调用方知道，但不抛
        logger.warning(
            "查事件邻域失败（将降级为「不判孤岛」）: %s namespace=%s", exc, namespace
        )
        return None
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)


# ---------------------------------------------------------------- A10 第 4 类硬信号

#: 查事件度数。**只数连到「非 Event」节点的边**。
#:
#: 为什么排除 `(e)--(:Event)`（spec 034 实测发现）：
#:
#: `graph_outbox` 会给「共享 Person / Entity」的两个 Event 连 `RELATED` 边，
#: 上限 `MAX_RELATED_LINKS=20`。于是一个只提到 1 个人的事件，只要那个人
#: 在 40 条记忆里都出现，它就能拿到 20 条 RELATED 边。
#:
#: 而 `GRAPH_HUB_DEGREE=8` 的意图是「**内容**特别丰富」（提到很多人/实体/动作），
#: 典型事件是 4-6 条结构边（AT + ACTION + MENTIONS + INVOLVES）。
#: 把 RELATED 算进去，结果是**几乎所有事件度数都 > 8** ->
#: 全部判 `graph_hub` 保护 -> **遗忘机制完全失效**。
#:
#: 实测：DingTalk 40 条真实记忆，度数分布 16-25，边类型
#: `{RELATED: 38, AT: 40, INVOLVES: 38, MENTIONS: 12, ACTION: 4}` ——
#: 排除 RELATED 之后单事件结构边只有 3-4 条，符合「典型 4-6」的假设。
#:
#: 注意：030 的端到端测试用的是 `TESTLINK` 连 Time/Action/Entity/Person，
#: 一条 Event-Event 边都没有，所以那批测试**测不出这个缺陷**。
#: 真实数据才暴露出来 —— 这是合成图的盲区。
_DEGREE_CYPHER = """
UNWIND $event_ids AS eid
MATCH (e:Event {namespace: $namespace, event_id: eid})
RETURN eid AS event_id,
       size([(e)--(n) WHERE NOT n:Event | 1]) AS degree
"""


def event_degrees(
    event_ids: list[str],
    namespace: str,
    *,
    settings: Any,
    driver: Any | None = None,
    timeout_s: float = 3.0,
) -> dict[str, int] | None:
    """批量查 `:Event` 节点的度（入边 + 出边数）。

    A10 第 4 类硬信号「图结构位置」的数据来源。

    **返回值有三种含义，不要混淆**：

    - `{...}`：正常，没查到的 event_id 不在字典里（调用方按 0 处理）
    - `None`：**图不可用**。调用方必须降级（当 0 用），并在响应里标注
      `graph_degraded=true` —— 不能把「查不了」当成「度为 0」静默处理

    Args:
        event_ids: 要查的 event_id 列表。
        namespace: 记忆域。
        settings: 提供 Neo4j 连接参数。
        driver: 可注入的 driver（测试用）。
        timeout_s: 连接超时。

    Returns:
        `{event_id: degree}`，或 `None` 表示图不可用。
    """
    if not event_ids:
        return {}

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
        with drv.session() as session:
            records = session.run(
                _DEGREE_CYPHER,
                event_ids=list(event_ids),
                namespace=namespace,
            )
            return {r["event_id"]: int(r["degree"]) for r in records}
    except Exception as exc:  # noqa: BLE001 - 图不可用必须让调用方知道，但不抛
        logger.warning("查事件度失败（将降级为 0）: %s namespace=%s", exc, namespace)
        return None
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)
