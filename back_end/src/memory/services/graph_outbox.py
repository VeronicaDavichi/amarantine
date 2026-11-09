"""B9 图 outbox：Event / Person / Time 三类核心节点，关系只追加不覆盖。

ADR D2 的红线是「**图可丢弃重建**」：

- 写入顺序：先 Weaviate 成功 → 再发图更新
- 图写失败**只告警，不回滚、不阻断**
- 节点/边统一带 `namespace`，禁止跨域

## 节点

| 节点 | 键 | 说明 |
|---|---|---|
| `(:Event)` | `namespace + event_id` | 事件本体，内容哈希做键，天然幂等 |
| `(:Person)` | `namespace + name` | **人**。是一等公民，不是「kind=person 的实体」 |
| `(:Time)` | `namespace + key` | **时间**。`YYYY-MM-DD`，让「那阵子发生了什么」能查 |
| `(:Entity)` | `namespace + name` | 非人实体（topic/place/org/event） |
| `(:Context)` | `namespace + key` | 当前话题 |

**为什么人要从 Entity 里拆出来**：人是记忆的主语，也是最常用的检索入口
（「张三的事」「我和李四之间」）。把它当成 `kind` 的一个取值，
就只能靠属性过滤找，没法作为图的枢纽去连。

**为什么时间要建节点**：`date_start` 只是 Event 的属性，属性没法被多张卡共享、
也没法做「同一时段」的聚合。建成节点后 `(Event)-[:AT]->(Time)` 才能
让「上个月」这种查询走索引，而不是扫全部事件。

## 关系

```
(Event)-[:INVOLVES {created_at}]->(Person)
(Event)-[:AT       {created_at}]->(Time)
(Event)-[:MENTIONS {created_at}]->(Entity)
(Event)-[:RELATED  {kind, created_at}]->(Event)
(Context)-[:RELATES_TO {created_at}]->(Event)
```

**关系一律 `CREATE`，绝不用 `MERGE`**：

`MERGE (a)-[r:RELATED]->(b) SET r.at = $now` 会抹掉上一次的时间。
同一条记忆被反复提起，恰恰是最有价值的信号 —— 覆盖掉历史等于把信号删了。
所以同一对节点之间可以有多条边，各自带 `created_at`，时间线完整保留。

节点本身用 `MERGE` 是安全的：它们的键（event_id / name / date）就是身份，
合并同身份的两个节点正是我们要的。

**Neo4j 不做忘却处理**：删卡时图上的边留在原地。

## `RELATED` 的产生条件

两个 Event 共享至少一个 Person **或** Entity 才连边。

「不同 event 之间必有关系」不等于「两两相连」—— 后者是 O(n²) 的边数爆炸。
共享同一个人的两件事、共享同一个项目的两件事，才是真的有关系。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from memory.config import Settings
from memory.core.entity_key import key_for
from memory.models.event_card import ALIAS_CONFIDENCE, AliasClaim, EventCard
from memory.services.labeler import _judge_name as judge_name

logger = logging.getLogger("memory.graph")

#: 一次写入最多连出多少条 event-event 边（防边数爆炸）
MAX_RELATED_LINKS = 20

#: Event 节点：`MERGE` 安全，event_id 是内容哈希，同一条内容不会有两份
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

#: 人：`kind == "person"` 的实体升级成独立的 Person 节点
#:
#: **按 `key` 建身份，不按 `name`**：名字是可改的显示名，代号才是身份。
#: 详见 `core/entity_key.py`。
#:
#: **MERGE 不带 label** —— 这是这一版最要紧的改动。原来写的是
#: `MERGE (p:Person {key})`，如果同一个 key 已经以 `:Entity` 存在
#: （别名声明建过、或抽取时被判成物），MERGE 不会匹配，直接新建一个
#: `:Person` —— 同一个实体在图上裂成两个，查询时各返回一半的边。
#: 去掉 label 后，任何标签的同 key 节点都能合并；`ON CREATE SET p:Person`
#: 只在**真的新建**时打标签，已存在的不动它的标签。
#:
#: `ON CREATE SET name` 而不是 `SET name`：重放不会把用户改过的名字顶回去，
#: 跟 upsert 的「重放可以修正事实，不能改写历史」是同一条规矩。
APPEND_PERSON_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
UNWIND $persons AS item
MERGE (p {namespace: $namespace, key: item.key})
ON CREATE SET p.name = item.name, p.first_seen = $created_at
SET p:Person
CREATE (e)-[:INVOLVES {created_at: $created_at}]->(p)
RETURN count(*)
"""

#: 别名：`(Person)-[:ALIAS_OF]->(Alias)`
#:
#: **一个人可能有多种称呼，一种称呼也可能对应多个人。**
#:
#: - `三哥` 既能指印度人，也能指家里排行第三的男性 ——
#:   所以 `(:Alias {name:"三哥"})` 可以被多条 `ALIAS_OF` 边指向。
#: - 召回时发现一个别名有多个主人，必须标 `ambiguous`，不能随便挑一个。
#:
#: **边用 CREATE 不用 MERGE**：同一条声明说两遍是两件事，
#: 而且 CREATE 才能带 `source_event_id` 追溯「哪句话说的」。
#: 撤销走 `DELETE`（按 `source_event_id` 或 `label` 精确删），
#: 原始事件卡不动 —— 别名是派生关系，删了不丢事实。
#: 别名：`(:Alias)` 记名字，宿主节点连 `ALIAS_OF` 过来。
#:
#: **宿主不再硬绑 `:Person`。** 原来写 `MERGE (p:Person {key})`，
#: 于是「豆包」（一个产品）被建成 Person，而实体抽取又把同名建成 Entity，
#: 同一个东西在图上裂成两份 —— `list_known_names` 里出现两次就是这么来的。
#:
#: 现在的规矩：别名只能说明「这两个名字指同一个东西」，**不说明那个东西
#: 是什么**。所以宿主按 label-free 合并，新建时默认 `:Entity`；
#: 如果实体抽取随后判定它是人，`APPEND_PERSON_CYPHER` 会补上 `:Person`
#: 标签而**不会**另建节点（因为 MERGE 也不带 label 了）。
APPEND_ALIAS_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
UNWIND $aliases AS item
MERGE (a:Alias {namespace: $namespace, name: item.alias})
ON CREATE SET a.first_seen = $created_at
MERGE (p {namespace: $namespace, key: item.key})
ON CREATE SET p.name = item.canonical, p.first_seen = $created_at, p:Entity
MERGE (p)-[r:ALIAS_OF]->(a)
ON CREATE SET r.created_at = $created_at,
              r.confidence = item.confidence,
              r.label = item.label,
              r.source = item.source,
              r.source_event_id = $event_id
ON MATCH SET r.label = item.label,
             r.confidence = item.confidence,
             r.source = item.source
RETURN count(*)
"""

#: 原来的关系写的是 `CREATE`，同一条声明重放就会多出一条平行边。
#: 正常写入链路靠 `event_id` 的幂等短路掩盖了这个 bug —— 同一句话不会
#: 走到第二次建边。但「人工裁决 accepted → 补建边」这条路径会重复调用
#: （补建失败可重试、人可能点两次），于是图上出现两条 `ALIAS_OF`，
#: 召回时同一条别名被算两遍。
#:
#: 所以关系改成 `MERGE`：**一对 (canonical, alias) 只保留一条边**。
#: `ON CREATE` 记首次的时间与来源事件，`ON MATCH` 只更新可变的判定属性
#: （`label` / `confidence` / `source`）—— 人工裁决把 `uncertain` 改成
#: `exact` 能覆盖上去，但 `created_at` / `source_event_id` 保留最早的。

#: 召回时从别名反查主人。一个别名可能有多个主人（一词多义）。
#:
#: 起点**不限定 label**：合并两个实体之后，被并掉的那个降级成 `(:Alias)`，
#: 边从存活的那个出发 —— 它可能是 `:Person`，也可能是 `:Entity`。
#: 写死 `(p:Person)` 会让「豆包」这种非人实体的别名查不出来。
#: 别名解析：**双向**（spec 038 A5）。
#:
#: 原来只认 `别名 -> 本名`（`a.name IN $names`），于是查「王泳」时
#: 找不到挂在「王老师」名下的事件 —— 而人的两个称呼本来就该互通。
#: 现在两个方向都解析，调用方把两侧名字都放进 `$alias_hosts`。
RESOLVE_ALIAS_CYPHER = """
MATCH (a:Alias {namespace: $namespace})<-[r:ALIAS_OF]-(p)
WHERE a.name IN $names OR p.name IN $names
RETURN a.name AS alias, p.name AS canonical,
       r.label AS label, r.confidence AS confidence,
       r.source_event_id AS source_event_id
"""

#: 给 LLM 别名推断用的候选名单。
#:
#: **只取 Person 和 Entity**：`(:Alias)` 不能当候选 —— 让 LLM 在别名里选，
#: 会把「糖包 -> 豆包」推成「糖包 -> 小豆包」，链路越绕越偏。
#: `(:Time)` 更不行，日期不可能是谁的别名。
#:
#: `DISTINCT` 不能省：同一个名字可能同时存在 `:Person` 和 `:Entity` 两个节点 ——
#: `APPEND_ALIAS_CYPHER` 为了建 `ALIAS_OF` 边会 `MERGE (p:Person)`，
#: 而实体抽取又会把同名名字建成 `:Entity`。这是图模型的既有问题
#: （别名的主人被硬绑成 Person，但「豆包」显然不是人），
#: 不是这里能修的。至少在候选列表里去重，别让 LLM 看到两个「豆包」。
LIST_KNOWN_NAMES_CYPHER = """
MATCH (n {namespace: $namespace})
WHERE n:Person OR n:Entity
RETURN DISTINCT n.name AS name
ORDER BY name
LIMIT $limit
"""

#: 动作：**按 verb 建唯一节点**，同义词已归并
#:
#: `(:Action {verb:"延期"})` 全库只有一个 —— 这样「哪些事被延过」是一次
#: 走边就能答的聚合查询。如果按 (verb, patient) 配对建节点，
#: 每条事件都会长出新节点，永远聚合不起来。
#:
#: **patient 必须放在边上，不能放到节点上**：
#: Action 节点是全库共享的，在它上面挂 `PATIENT` 边会让所有事件的宾语
#: 挤成一个集合 —— `李四推迟了项目B` 和 `张三延期项目A` 会互相污染，
#: 聚合查询直接答错。`ACTION` 边带事件上下文，放这里才不串。
APPEND_ACTION_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
UNWIND $actions AS item
MERGE (a:Action {namespace: $namespace, verb: item.verb})
ON CREATE SET a.first_seen = $created_at
CREATE (e)-[:ACTION {
    created_at: $created_at,
    primary: item.primary,
    patient: item.patient
}]->(a)
RETURN count(*)
"""

#: 非人实体：topic / place / org / event
#:
#: 同样 label-free 合并：一个 key 在图上只能有一个节点，
#: 至于它带哪些 label，由第一次创建它的人决定。
APPEND_ENTITY_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
UNWIND $entities AS item
MERGE (n {namespace: $namespace, key: item.key})
ON CREATE SET n.name = item.name, n.kind = item.kind
SET n:Entity
CREATE (e)-[:MENTIONS {created_at: $created_at}]->(n)
RETURN count(*)
"""

#: 时间：按 `date_start` 建日粒度节点，让「那阵子」能走索引
APPEND_TIME_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MERGE (t:Time {namespace: $namespace, key: $time_key})
ON CREATE SET t.year = $time_year, t.month = $time_month
CREATE (e)-[:AT {created_at: $created_at}]->(t)
RETURN t
"""

#: event-event 边：共享 Person 或 Entity 的两个 Event 连一条，**只追加**
LINK_RELATED_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MATCH (e)-[:MENTIONS|INVOLVES]->()<-[:MENTIONS|INVOLVES]-(other:Event {namespace: $namespace})
WHERE other.event_id <> e.event_id
WITH DISTINCT e, other
LIMIT $max_links
CREATE (e)-[:RELATED {kind: 'shared_node', created_at: $created_at}]->(other)
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
    #: 本次涉及的人 / 时间键 / 非人实体数（诊断用）
    entities: int = 0
    persons: int = 0
    time_key: str | None = None
    #: 本次涉及的动作数（诊断用）
    actions: int = 0
    #: 本次建立的别名边数（只算右边像名字的声明）
    aliases: int = 0
    #: **右值被判据拒掉、因此没建边的声明**。带理由，由 `remember`
    #: 落进 `alias_reject` 表 —— 不留痕的话，「边为什么没建」只能靠猜，
    #: 而 `印度人`/`科学家` 两个 bug 都是这么藏了两个会话的。
    alias_skipped: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class AliasSkip:
    """一条因为右值不合判据而没建边的声明。"""

    alias: str
    canonical: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "canonical": self.canonical,
            "side": "canonical",
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AliasWriteResult:
    """`append_alias_edges` 的结果：建了几条 + 拒了哪些。

    支持跟 `int` 比较（`result == 1`），这样老测试和老调用方的
    数值判断习惯还能用 —— 但**不要**再用它做算术。
    """

    written: int = 0
    skipped: list[AliasSkip] = field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.written == other
        if not isinstance(other, AliasWriteResult):
            return NotImplemented
        return self.written == other.written and self.skipped == other.skipped

    def __hash__(self) -> int:
        return hash((self.written, tuple(self.skipped)))

    def __int__(self) -> int:
        return self.written


def _alias_item(claim: AliasClaim) -> dict[str, str]:
    return {
        "alias": claim.alias,
        "canonical": claim.canonical,
        "key": key_for(claim.canonical),
        "label": claim.label,
        "confidence": ALIAS_CONFIDENCE.get(claim.label, 0.4),
        "source": claim.source,
    }


def _partition_alias_claims(
    claims: list[AliasClaim],
) -> tuple[list[dict[str, str]], list[AliasSkip]]:
    """把别名声明分成「能建边」和「被拒」两堆，被拒的带理由。

    两条建边路径（事件写入 / LLM 推断）共用这一个切分，
    这样「为什么没建」的答案只写一遍。
    """
    items: list[dict[str, str]] = []
    skipped: list[AliasSkip] = []
    for c in claims:
        if not c.alias:
            skipped.append(
                AliasSkip(c.alias, c.canonical, "empty_alias")
            )
            continue
        verdict = judge_name(c.canonical)
        if not verdict.ok:
            skipped.append(
                AliasSkip(
                    alias=c.alias,
                    canonical=c.canonical,
                    reason=verdict.reason,
                    detail=verdict.detail,
                )
            )
            continue
        items.append(_alias_item(c))
    return items, skipped


def _split_entities(
    card: EventCard,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """把人从实体里分出来：人走 Person 节点，其余走 Entity 节点。

    同一个人和一个话题不该混在同一个标签下 —— 前者是记忆的主语，
    后者是记忆的宾语，查询入口完全不同。

    返回的每一项都是 `{key, name}`：**`key` 是身份，`name` 是显示名**。
    同名默认同 key（视为同一实体），这是 `key_for` 的确定性行为。
    """
    persons: list[dict[str, str]] = []
    entities: list[dict[str, str]] = []
    for e in card.entities:
        item = {"key": key_for(e.name), "name": e.name}
        if e.kind == "person":
            persons.append(item)
        else:
            entities.append({**item, "kind": e.kind})
    return persons, entities


def write_event_node(
    card: EventCard,
    *,
    settings: Settings,
    weaviate_id: str | None,
    context: str | None = None,
    driver: object | None = None,
) -> OutboxResult:
    """把事件卡同步成图节点，并**追加**这次提及产生的关系。

    六步顺序不能换：

    1. `MERGE` Event 节点
    2. `CREATE` Person 边（`kind=person`）
    3. `CREATE` Entity 边（其余 kind）
    4. `CREATE` Time 边（按 `date_start` 的日粒度）
    5. `CREATE` event-event 边 —— 依赖第 2/3 步建好的边
    6. `CREATE` Context 边（可选）

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

        day = card.date_start
        params = {
            "namespace": card.namespace,
            "event_id": card.event_id,
            "summary": card.summary,
            "date_start": day.isoformat(),
            "date_end": card.date_end.isoformat() if card.date_end else None,
            "types": list(card.types),
            "importance": card.importance,
            "pinned": card.pinned,
            "retention": card.retention,
            "weaviate_id": weaviate_id,
            "ingested_at": card.ingested_at.isoformat(),
            # 关系的统一时间戳：这次「提及」发生的时刻
            "created_at": card.ingested_at.isoformat(),
            # 时间节点：日粒度，月和年是给「按月/按年」查询用的副本属性
            "time_key": day.isoformat(),
            "time_year": day.year,
            "time_month": f"{day.year:04d}-{day.month:02d}",
        }
        persons, entities = _split_entities(card)
        action_items = [
            {"verb": a.verb, "primary": a.primary, "patient": a.patient}
            for a in card.actions
        ]

        related = 0
        with drv.session() as session:
            session.run(MERGE_EVENT_CYPHER, **params).consume()

            if persons:
                session.run(APPEND_PERSON_CYPHER, **params, persons=persons).consume()

            if entities:
                session.run(
                    APPEND_ENTITY_CYPHER, **params, entities=entities
                ).consume()

            if action_items:
                session.run(
                    APPEND_ACTION_CYPHER, **params, actions=action_items
                ).consume()

            session.run(APPEND_TIME_CYPHER, **params).consume()

            if persons or entities:
                record = session.run(
                    LINK_RELATED_CYPHER,
                    **params,
                    max_links=MAX_RELATED_LINKS,
                ).single()
                related = int(record[0]) if record and record[0] is not None else 0

            # 别名：只建「右边像名字」的那些声明。
            # 描述性的（`糖包就是那个AI里最弱智的`）不建边 ——
            # 把描述塞进 (:Alias) 节点，以后查「糖包」会拉出一个叫
            # 「那个AI里最弱智的」的人。这类 claim 留在事件卡上给 LLM 层解析。
            #
            # **被拒的不丢**：`alias_skipped` 带理由带出去，由 `remember`
            # 落进 `alias_reject` 表。判据可能错（`印度人`/`科学家` 都是），
            # 错了要能看见、能修。
            alias_items, alias_skipped = _partition_alias_claims(
                list(card.alias_claims)
            )
            if alias_items:
                session.run(
                    APPEND_ALIAS_CYPHER, **params, aliases=alias_items
                ).consume()

            if context:
                session.run(LINK_CONTEXT_CYPHER, **params, context=context).consume()

        return OutboxResult(
            ok=True,
            related_links=related,
            persons=len(persons),
            entities=len(entities),
            time_key=params["time_key"],
            actions=len(action_items),
            aliases=len(alias_items),
            alias_skipped=tuple(s.to_dict() for s in alias_skipped),
        )
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


# ---------------------------------------------------------------- 别名推断

def list_known_names(
    namespace: str,
    *,
    settings: Settings,
    limit: int = 200,
    driver: object | None = None,
) -> list[str]:
    """取图上已有的 Person / Entity 名字，供 LLM 做别名消歧。

    **只给名字，不给整张卡**：让 LLM 在几十个名字里做选择题，
    比喂它一堆句子更准，也省上下文。

    查不到就返回空列表，**不抛异常** —— 推断是增强功能，
    图挂了不该让调用方跟着挂。
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
        with drv.session() as session:  # type: ignore[union-attr]
            rows = list(
                session.run(
                    LIST_KNOWN_NAMES_CYPHER, namespace=namespace, limit=int(limit)
                )
            )
        return [r["name"] for r in rows if r.get("name")]
    except Exception as exc:  # noqa: BLE001 - 推断失败只告警
        logger.warning("读取已知名字失败（别名推断将跳过）: %s", exc)
        return []
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass


def append_alias_edges(
    event_id: str,
    namespace: str,
    claims: list[AliasClaim],
    *,
    settings: Settings,
    created_at: str,
    driver: object | None = None,
    force: bool = False,
) -> AliasWriteResult:
    """把别名边追加到图上，返回建了几条 + 拒了哪些。

    走的是同一个 `APPEND_ALIAS_CYPHER` —— 推断出来的边和用户声明的边
    在图上结构完全一致，区别只在 `source` / `label` 两个属性。
    这样召回侧不需要区分「这条边谁建的」，标记由属性自己带出。

    ## 从 `int` 改成结果对象的原因

    **右值是描述性短语时不建边，但 claim 留在卡上。** 这个过滤是
    「预期内的丢弃」，不是兜底：`糖包就是那个ai中最弱智的` 会留下
    `canonical="AI里最弱智的"`，等 LLM 层去解析，规则层不猜。

    但原来这个过滤只返回一个数字，**拒了哪些一句话都没有**。判据要是
    把真名字误判成描述（`印度人` 就是），边一条建不出来且毫无痕迹。
    现在把 `skipped` 一起返回，调用方落进 `alias_reject` 表。

    ## `force=True` 绕过的正是上面这道判据

    人工在 `/admin/alias-rejects/resolve` 上标了 `accepted`，意思是
    「**判据错了**，这条声明是真名字」。这时候如果还走
    `_partition_alias_claims`，同一道判据会把边再拦一次 —— 人已经
    否决了判据，结果图上仍然没边，而且这次连 `skipped` 都不会有人看。
    那正好是 002 要消灭的形态。

    所以 `force=True` 时不判、不跳：直接 `_alias_item()` 装参数，
    `skipped` 恒为空。**这是唯一一处可以绕过判据的入口**，只给人工
    裁决用；正常写入链路（`force=False`）行为不变。
    """
    if force:
        items, skipped = [_alias_item(c) for c in claims], []
    else:
        items, skipped = _partition_alias_claims(list(claims))
    if not items:
        return AliasWriteResult(written=0, skipped=skipped)

    owns_driver = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
        with drv.session() as session:  # type: ignore[union-attr]
            session.run(
                APPEND_ALIAS_CYPHER,
                namespace=namespace,
                event_id=event_id,
                created_at=created_at,
                aliases=items,
            )
        return AliasWriteResult(written=len(items), skipped=skipped)
    except Exception as exc:  # noqa: BLE001 - 图失败只告警
        logger.warning("追加别名边失败（已忽略）: %s", exc)
        return AliasWriteResult(written=0, skipped=skipped)
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
