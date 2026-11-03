"""实体纠错：改 / 拆 / 合 / 解挂重挂。**不提供删除。**

## 为什么没有「删」

删掉一个抽错的节点，事件卡里那句原文还写着它，下次重放还会抽出来，
而且删完就无从追溯「这里曾经抽错过」。这跟 N4「事实层永不物理删除」
是同一条原则：**纠错是修正，不是抹除痕迹**。

四种操作覆盖了所有真正的纠错需求：

    改     rename   名字写错了        `助手` → `豆包`
    拆     split    一个节点两个东西   `张三李四` → `张三` + `李四`
    合     merge    两个节点一个东西   `小张` + `张三` → 张三（小张降级为别名）
    解挂   rebind   **这句的指向错了** 这条事件的「三哥」其实是印度人

## 「改」和「解挂」不是一回事

这是最容易混的一对：

    改   节点该叫什么。`助手` 抽错了名字，改成 `豆包`，所有边跟着走。
         改完 `助手` 这个名字就不存在了。

    解挂 **这一句在说谁**。`三哥` 这个名字没错，别的句子里它确实指哥哥；
         只是**这一句**说的是印度人。所以只动这一条事件的边，
         别名层（三哥 → 哥哥 / 印度人 两条边）完全不动。

混成一个操作的话，要么别的句子跟着错，要么这一句改不掉。

## 为什么「改」不需要处理冲突

节点身份是 `key`（见 `core/entity_key.py`），`name` 只是显示名。
`MERGE` 按 `key` 走，所以改 `name` 跟别的节点叫什么完全无关 ——
两个实体都叫「豆包」是合法状态，各有各的 key。

这就是「重名就把名字换成代号存进数据库，读出时原文替代」：
代号是 key，原文是 name，**改`name`永远不冲突**。

## 边只涉及两种

实体节点上的边只有 `(Event)-[:INVOLVES]->(:Person)` 和
`(Event)-[:MENTIONS]->(:Entity)`。其余边（`ACTION` / `AT` / `RELATED` /
`RELATES_TO` / `ALIAS_OF`）都从 Event 或 Alias 出发，搬实体用不到。

Cypher 不支持动态关系类型，所以这两种要分开写 —— 这是硬限制，不是懒。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from neo4j import GraphDatabase

from memory.config import Settings
from memory.core.entity_key import fresh_key, key_for

logger = logging.getLogger("memory.entity_edit")

#: 实体节点上的两种边。搬边时按类型逐个处理（Cypher 不能动态指定关系类型）
ENTITY_EDGE_TYPES = ("INVOLVES", "MENTIONS")

#: 可能出现在实体节点上的标签
ENTITY_LABELS = ("Person", "Entity")


@dataclass
class EditResult:
    """一次纠错的结果。"""

    ok: bool
    op: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "op": self.op,
            "detail": self.detail,
            "error": self.error,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: 每次改/拆/合都留一条痕。谁、何时、做了什么、为什么。
#:
#: 留在图上而不是 SQLite：纠错本来就是图的操作，重建图时留痕不该先丢。
#:
#: `args` 必须是 JSON 字符串 —— Neo4j 的属性值只接受基本类型和它们的数组，
#: 嵌套 Map 会直接报 `CypherTypeError`。这事儿不加注释下次还会踩。
RECORD_EDIT_CYPHER = """
CREATE (ed:EntityEdit {
    namespace: $namespace,
    op: $op,
    target: $target,
    args: $args,
    reason: $reason,
    operator: $operator,
    created_at: $created_at
})
RETURN ed.created_at
"""

#: 改名：只动 `name`，`key` 和所有边都不受影响。
RENAME_CYPHER = """
MATCH (n {namespace: $namespace, name: $from})
SET n.name = $to
RETURN count(n) AS renamed
"""

#: 找出一个名字对应的所有节点（可能多个：同名不同 key）。
FIND_BY_NAME_CYPHER = """
MATCH (n {namespace: $namespace, name: $name})
WHERE n:Person OR n:Entity
RETURN n.key AS key, n.name AS name, labels(n) AS labels
"""

#: 合并第一步：把 src 出边（Event 指向 src）改成指向 dst。
#: 关系类型不能动态，两条分别写。
MOVE_INVOLVES_CYPHER = """
MATCH (e:Event {namespace: $namespace})-[r:INVOLVES]->(src {namespace: $namespace, key: $src_key})
MATCH (dst {namespace: $namespace, key: $dst_key})
CREATE (e)-[:INVOLVES {created_at: r.created_at}]->(dst)
RETURN count(*)
"""

MOVE_MENTIONS_CYPHER = """
MATCH (e:Event {namespace: $namespace})-[r:MENTIONS]->(src {namespace: $namespace, key: $src_key})
MATCH (dst {namespace: $namespace, key: $dst_key})
CREATE (e)-[:MENTIONS {created_at: r.created_at}]->(dst)
RETURN count(*)
"""

#: 合并第二步：src 降级成 Alias，并连一条 ALIAS_OF 到 dst。
#:
#: **不删 src**：它的 key 和 name 都留着，作为「曾经这么叫过」的证据。
#: 边的属性里记 `source="user_merge"`，跟 LLM 推断的分得开。
DEMOTE_TO_ALIAS_CYPHER = """
MATCH (src {namespace: $namespace, key: $src_key})
MATCH (dst {namespace: $namespace, key: $dst_key})
SET src:Alias
REMOVE src:Person
REMOVE src:Entity
CREATE (dst)-[:ALIAS_OF {
    created_at: $created_at,
    confidence: 1.0,
    label: $label,
    source: $source,
    source_event_id: $source_event_id
}]->(src)
RETURN src.name AS alias, dst.name AS canonical
"""

#: 拆分：原节点记下 split_into，不删。
MARK_SPLIT_CYPHER = """
MATCH (n {namespace: $namespace, name: $from})
SET n.split_into = $into, n.split_at = $created_at
RETURN count(n) AS marked
"""

#: 拆分：为每个子实体建节点（已存在就复用），并把原节点的边**两边都连**。
#:
#: 为什么两边都连：原文确实写的是「张三李四」，两个人都算被提及是诚实的。
#: 边上标 `from_split=true`，召回时能看出这条关联不精确。
LINK_SPLIT_CHILD_CYPHER = """
MATCH (e:Event {namespace: $namespace})-[r:INVOLVES]->(src {namespace: $namespace, key: $src_key})
MATCH (child {namespace: $namespace, key: $child_key})
CREATE (e)-[:INVOLVES {created_at: r.created_at, from_split: true}]->(child)
RETURN count(*)
"""

LINK_SPLIT_CHILD_MENTIONS_CYPHER = """
MATCH (e:Event {namespace: $namespace})-[r:MENTIONS]->(src {namespace: $namespace, key: $src_key})
MATCH (child {namespace: $namespace, key: $child_key})
CREATE (e)-[:MENTIONS {created_at: r.created_at, from_split: true}]->(child)
RETURN count(*)
"""

#: 拆分：确保子实体节点存在
ENSURE_CHILD_CYPHER = """
MERGE (c:Entity {namespace: $namespace, key: $key})
ON CREATE SET c.name = $name, c.first_seen = $created_at
RETURN c.key AS key, c.name AS name
"""

# ---------------------------------------------------------------- 解挂重挂

#: 解挂：删掉这条事件指向旧实体的边。
#:
#: 只删**这一条事件的边**，节点不动、别名层不动 ——
#: `三哥` 这个名字没错，别的句子里它确实指哥哥。
UNBIND_INVOLVES_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
      -[r:INVOLVES]->
      (old {namespace: $namespace, name: $from_name})
DELETE r
RETURN count(*)
"""

UNBIND_MENTIONS_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
      -[r:MENTIONS]->
      (old {namespace: $namespace, name: $from_name})
DELETE r
RETURN count(*)
"""

#: 重挂：把这条事件挂到新实体上。
#:
#: 目标可能还不存在（`印度人` 从来没单独出现过，只作为别名右值来过）——
#: 所以先 MERGE 建档。`kind` 由调用方给：这里收的是「这句到底在说谁」，
#: 人还是物得人来说，系统不猜。
REBIND_INVOLVES_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MERGE (new:Person {namespace: $namespace, key: $to_key})
ON CREATE SET new.name = $to_name, new.first_seen = $created_at
CREATE (e)-[:INVOLVES {created_at: $created_at, from_rebind: true}]->(new)
RETURN new.key AS key, new.name AS name
"""

REBIND_MENTIONS_CYPHER = """
MATCH (e:Event {namespace: $namespace, event_id: $event_id})
MERGE (new:Entity {namespace: $namespace, key: $to_key})
ON CREATE SET new.name = $to_name, new.kind = $to_kind, new.first_seen = $created_at
CREATE (e)-[:MENTIONS {created_at: $created_at, from_rebind: true}]->(new)
RETURN new.key AS key, new.name AS name
"""

#: 解挂时如果旧节点已经没人引用，把这件事记在它身上（**不删节点**）。
#:
#: 「没人引用了」不等于「可以删」—— 它可能是别的实体的别名，
#: 也可能哪天补录时又被提到。留着比删掉安全。
MARK_ORPHAN_CYPHER = """
MATCH (n {namespace: $namespace, name: $name})
WHERE n:Person OR n:Entity
OPTIONAL MATCH (e:Event)-[:INVOLVES|MENTIONS]->(n)
WITH n, count(e) AS refs
SET n.orphan_since = CASE WHEN refs = 0 THEN $created_at ELSE n.orphan_since END
RETURN n.name AS name, refs
"""


class EntityEditError(Exception):
    """纠错操作失败。"""


def _driver_for(settings: Settings, driver: object | None):
    if driver is not None:
        return driver, False
    return (
        GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        ),
        True,
    )


def _find(drv, namespace: str, name: str) -> list[dict[str, Any]]:
    with drv.session() as session:  # type: ignore[union-attr]
        rows = session.run(FIND_BY_NAME_CYPHER, namespace=namespace, name=name)
        return [
            {"key": r["key"], "name": r["name"], "labels": list(r["labels"])}
            for r in rows
        ]


def _record(
    drv,
    *,
    namespace: str,
    op: str,
    target: str,
    args: dict[str, Any],
    reason: str,
    operator: str,
) -> None:
    with drv.session() as session:  # type: ignore[union-attr]
        session.run(
            RECORD_EDIT_CYPHER,
            namespace=namespace,
            op=op,
            target=target,
            # Neo4j 属性只收基本类型：嵌套 dict 会 CypherTypeError
            args=json.dumps(args, ensure_ascii=False, default=str),
            reason=reason,
            operator=operator,
            created_at=_now(),
        ).consume()


# ---------------------------------------------------------------- 改

def rename_entity(
    namespace: str,
    from_name: str,
    to_name: str,
    *,
    settings: Settings,
    reason: str = "",
    operator: str = "admin",
    driver: object | None = None,
) -> EditResult:
    """把实体的显示名从 `from_name` 改成 `to_name`。

    **无条件成功**：`key` 是身份，`name` 只是显示名。改完如果有另一个
    实体也叫 `to_name`，那是合法的「重名」状态 —— 两个节点各有各的 key，
    查询时都会返回，读出时都显示原文。
    """
    if not from_name or not to_name:
        return EditResult(False, "rename", error="名字不能为空")
    if from_name == to_name:
        return EditResult(False, "rename", error="新旧名字相同")

    drv, owns = _driver_for(settings, driver)
    try:
        found = _find(drv, namespace, from_name)
        if not found:
            return EditResult(
                False, "rename", error=f"图上没有叫「{from_name}」的实体"
            )

        with drv.session() as session:  # type: ignore[union-attr]
            row = session.run(
                RENAME_CYPHER, namespace=namespace, **{"from": from_name, "to": to_name}
            ).single()
        renamed = row["renamed"] if row else 0

        _record(
            drv,
            namespace=namespace,
            op="rename",
            target=from_name,
            args={"to": to_name, "count": renamed},
            reason=reason,
            operator=operator,
        )
        logger.info(
            "改名 %s -> %s（%d 个节点）| ns=%s", from_name, to_name, renamed, namespace
        )
        return EditResult(
            True,
            "rename",
            detail={
                "from": from_name,
                "to": to_name,
                "renamed": renamed,
                "keys": [f["key"] for f in found],
            },
        )
    finally:
        if owns:
            drv.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------- 合

def merge_entities(
    namespace: str,
    from_name: str,
    to_name: str,
    *,
    settings: Settings,
    reason: str = "",
    operator: str = "admin",
    label: str = "exact",
    driver: object | None = None,
) -> EditResult:
    """把 `from_name` 并进 `to_name`：边搬过去，被并的那个降级成别名。

    **不删被并的节点** —— 它的 key 和 name 留着，作为「曾经这么叫过」的证据，
    同时也让 `ALIAS_OF` 边上有个实体可以指。
    """
    if from_name == to_name:
        return EditResult(False, "merge", error="不能跟自己合并")

    drv, owns = _driver_for(settings, driver)
    try:
        srcs = _find(drv, namespace, from_name)
        dsts = _find(drv, namespace, to_name)
        if not srcs:
            return EditResult(False, "merge", error=f"图上没有叫「{from_name}」的实体")
        if not dsts:
            return EditResult(False, "merge", error=f"图上没有叫「{to_name}」的实体")

        src_key = srcs[0]["key"]
        dst_key = dsts[0]["key"]
        moved = {"involves": 0, "mentions": 0}

        with drv.session() as session:  # type: ignore[union-attr]
            moved["involves"] = (
                session.run(
                    MOVE_INVOLVES_CYPHER,
                    namespace=namespace,
                    src_key=src_key,
                    dst_key=dst_key,
                ).single() or {"count(*)": 0}
            )["count(*)"]
            moved["mentions"] = (
                session.run(
                    MOVE_MENTIONS_CYPHER,
                    namespace=namespace,
                    src_key=src_key,
                    dst_key=dst_key,
                ).single() or {"count(*)": 0}
            )["count(*)"]
            session.run(
                DEMOTE_TO_ALIAS_CYPHER,
                namespace=namespace,
                src_key=src_key,
                dst_key=dst_key,
                created_at=_now(),
                label=label,
                source="user_merge",
                source_event_id=f"merge:{from_name}->{to_name}",
            ).consume()

        _record(
            drv,
            namespace=namespace,
            op="merge",
            target=from_name,
            args={"into": to_name, "moved": moved, "label": label},
            reason=reason,
            operator=operator,
        )
        logger.info("合并 %s -> %s | ns=%s", from_name, to_name, namespace)
        return EditResult(
            True,
            "merge",
            detail={
                "from": from_name,
                "to": to_name,
                "moved": moved,
                "demoted_key": src_key,
                "surviving_key": dst_key,
            },
        )
    finally:
        if owns:
            drv.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------- 拆

def split_entity(
    namespace: str,
    from_name: str,
    into: list[str],
    *,
    settings: Settings,
    reason: str = "",
    operator: str = "admin",
    driver: object | None = None,
) -> EditResult:
    """把一个实体拆成多个。

    原节点**不删**，标记 `split_into` 留痕。子实体各自建节点，
    原节点的边**两边都连**并标 `from_split=true` ——
    原文写的是「张三李四」，两个人都算被提及是诚实的近似。
    """
    if not into or len(into) < 2:
        return EditResult(False, "split", error="至少拆成两个")
    if from_name in into:
        return EditResult(False, "split", error="拆分目标不能包含原名字")

    drv, owns = _driver_for(settings, driver)
    try:
        found = _find(drv, namespace, from_name)
        if not found:
            return EditResult(False, "split", error=f"图上没有叫「{from_name}」的实体")
        src_key = found[0]["key"]
        now = _now()
        children: list[dict[str, Any]] = []

        with drv.session() as session:  # type: ignore[union-attr]
            for name in into:
                ckey = key_for(name)
                session.run(
                    ENSURE_CHILD_CYPHER,
                    namespace=namespace,
                    key=ckey,
                    name=name,
                    created_at=now,
                ).consume()
                session.run(
                    LINK_SPLIT_CHILD_CYPHER,
                    namespace=namespace,
                    src_key=src_key,
                    child_key=ckey,
                ).consume()
                session.run(
                    LINK_SPLIT_CHILD_MENTIONS_CYPHER,
                    namespace=namespace,
                    src_key=src_key,
                    child_key=ckey,
                ).consume()
                children.append({"key": ckey, "name": name})

            session.run(
                MARK_SPLIT_CYPHER,
                namespace=namespace,
                **{"from": from_name, "into": into, "created_at": now},
            ).consume()

        _record(
            drv,
            namespace=namespace,
            op="split",
            target=from_name,
            args={"into": into},
            reason=reason,
            operator=operator,
        )
        logger.info("拆分 %s -> %s | ns=%s", from_name, into, namespace)
        return EditResult(
            True,
            "split",
            detail={"from": from_name, "into": into, "children": children},
        )
    finally:
        if owns:
            drv.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------- 解挂重挂

def rebind_event_entity(
    namespace: str,
    event_id: str,
    from_name: str,
    to_name: str,
    *,
    settings: Settings,
    to_kind: str = "topic",
    reason: str = "",
    operator: str = "admin",
    driver: object | None = None,
) -> EditResult:
    """把**某一条事件**对实体的引用从 `from_name` 改到 `to_name`。

    场景：`三哥主打的就是一个有声有色有味`，被认定这个「三哥」是印度人
    而不是哥哥。要解挂旧的、挂上新的 —— 但**只影响这一条事件**。

    为什么不能顺手用 `rename`：`三哥` 这个名字没错，别的句子里它确实
    指哥哥。改名会把所有句子一起改错。

    为什么旧节点不删：它可能还是别的句子的主语。删了那句就悬空了。
    如果确实没人引用，只记 `orphan_since` 标记，留给后续处理。

    Args:
        to_kind: 目标实体的类别。`person` 走 `INVOLVES`，其余走 `MENTIONS`。
            **系统不猜** —— 「三哥」到底是人还是国家，只有说的人知道。
    """
    if not from_name or not to_name:
        return EditResult(False, "rebind", error="名字不能为空")
    if from_name == to_name:
        return EditResult(False, "rebind", error="新旧指向相同，没什么可改")

    drv, owns = _driver_for(settings, driver)
    try:
        now = _now()
        unbound = {"involves": 0, "mentions": 0}
        bound: dict[str, Any] | None = None

        with drv.session() as session:  # type: ignore[union-attr]
            # 先确认这条事件确实提到了旧实体 —— 否则重挂就是凭空造关系
            old_rows = list(
                session.run(
                    """
                    MATCH (e:Event {namespace: $namespace, event_id: $event_id})
                          -[:INVOLVES|MENTIONS]->
                          (old {namespace: $namespace, name: $from_name})
                    RETURN count(*) AS c
                    """,
                    namespace=namespace,
                    event_id=event_id,
                    from_name=from_name,
                )
            )
            if not old_rows or old_rows[0]["c"] == 0:
                return EditResult(
                    False,
                    "rebind",
                    error=f"事件 {event_id} 没有提到「{from_name}」，无法解挂",
                )

            unbound["involves"] = (
                session.run(
                    UNBIND_INVOLVES_CYPHER,
                    namespace=namespace,
                    event_id=event_id,
                    from_name=from_name,
                ).single()
                or {"count(*)": 0}
            )["count(*)"]
            unbound["mentions"] = (
                session.run(
                    UNBIND_MENTIONS_CYPHER,
                    namespace=namespace,
                    event_id=event_id,
                    from_name=from_name,
                ).single()
                or {"count(*)": 0}
            )["count(*)"]

            to_key = key_for(to_name)
            if to_kind == "person":
                row = session.run(
                    REBIND_INVOLVES_CYPHER,
                    namespace=namespace,
                    event_id=event_id,
                    to_key=to_key,
                    to_name=to_name,
                    created_at=now,
                ).single()
            else:
                row = session.run(
                    REBIND_MENTIONS_CYPHER,
                    namespace=namespace,
                    event_id=event_id,
                    to_key=to_key,
                    to_name=to_name,
                    to_kind=to_kind,
                    created_at=now,
                ).single()
            if row:
                bound = {"key": row["key"], "name": row["name"]}

            orphan = session.run(
                MARK_ORPHAN_CYPHER,
                namespace=namespace,
                name=from_name,
                created_at=now,
            ).single()

        _record(
            drv,
            namespace=namespace,
            op="rebind",
            target=f"{event_id}:{from_name}",
            args={"to": to_name, "to_kind": to_kind, "unbound": unbound},
            reason=reason,
            operator=operator,
        )
        logger.info(
            "解挂重挂 %s/%s -> %s | ns=%s", event_id, from_name, to_name, namespace
        )
        return EditResult(
            True,
            "rebind",
            detail={
                "event_id": event_id,
                "from": from_name,
                "to": to_name,
                "to_kind": to_kind,
                "unbound": unbound,
                "bound": bound,
                "old_refs_left": orphan["refs"] if orphan else None,
            },
        )
    finally:
        if owns:
            drv.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------- 查痕迹


#: 查纠错痕迹。`op` / `target` 可选收窄；条件走参数，不拼字符串。
LIST_EDITS_CYPHER = """
MATCH (ed:EntityEdit {namespace: $namespace})
WHERE ($op IS NULL OR ed.op = $op)
  AND ($target IS NULL OR ed.target = $target)
RETURN ed.op AS op, ed.target AS target, ed.args AS args,
       ed.reason AS reason, ed.operator AS operator,
       ed.created_at AS created_at
ORDER BY ed.created_at DESC
LIMIT $limit
"""


def list_edits(
    namespace: str,
    *,
    settings: Settings,
    op: str | None = None,
    target: str | None = None,
    limit: int = 50,
    driver: object | None = None,
) -> dict[str, Any]:
    """列出纠错痕迹，按时间倒序。只读。

    `args` 在图上是 **JSON 字符串**（Neo4j 属性不收嵌套 Map），但查历史的
    人要看懂，所以出参解析成对象。解析失败**不吞**：原样返回字符串并标
    `args_parse_error`，让调用方知道自己看到的是残缺数据。
    """
    drv, owns = _driver_for(settings, driver)
    try:
        with drv.session() as session:  # type: ignore[union-attr]
            rows = session.run(
                LIST_EDITS_CYPHER,
                namespace=namespace,
                op=op,
                target=target,
                limit=int(limit),
            )
            items: list[dict[str, Any]] = []
            for r in rows:
                raw = r["args"]
                parsed: Any = raw
                parse_error = False
                try:
                    parsed = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    parse_error = True
                item: dict[str, Any] = {
                    "op": r["op"],
                    "target": r["target"],
                    "args": parsed,
                    "reason": r["reason"],
                    "operator": r["operator"],
                    "created_at": r["created_at"],
                }
                if parse_error:
                    item["args_parse_error"] = True
                items.append(item)
        return {"items": items, "total": len(items)}
    finally:
        if owns:
            drv.close()  # type: ignore[union-attr]
