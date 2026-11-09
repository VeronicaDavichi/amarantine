#!/usr/bin/env python3
"""spec 045 T5：把「按名字建的 Person」合并到「按 uid 建的 Person」。

## 什么时候需要

| 场景 | 是否需要本脚本 |
|---|---|
| 清空重灌（当前决定） | **不需要** —— 新写入直接用 `uid:` 键 |
| 保留旧图 | 需要 —— 名字键的老节点要并到 uid 键上 |

## 做法

1. 从 `(:Account)` 拿 `uid` 与 `nicks`
2. 对每个 uid：找出图上 `name ∈ nicks` 的 `Person`（名字键）
3. 目标 = `uid:<uid>` 节点（没有就新建）
4. 旧节点的边（`INVOLVES` / `MENTIONS` / `ALIAS_OF`）逐条重建到目标
5. 旧名并入 `previous_names`，删除旧节点

默认 dry-run；`--apply` 才改；`--backup FILE` 先把受影响子图导出（可回滚）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402

GROUPS_CYPHER = """
MATCH (a:Account)
WHERE coalesce(a.nicks, []) <> []
RETURN a.namespace AS ns, a.uid AS uid, a.nicks AS nicks
"""

CANDIDATES_CYPHER = """
MATCH (p:Person {namespace: $ns})
WHERE p.name IN $names AND NOT p.key STARTS WITH 'uid:'
RETURN p.key AS key, p.name AS name,
       size([(p)-[:INVOLVES]-() | 1]) AS involves,
       size([(p)-[:MENTIONS]-() | 1]) AS mentions,
       size([(p)-[:ALIAS_OF]-() | 1]) AS aliases
"""

BACKUP_CYPHER = """
MATCH (p:Person {namespace: $ns}) WHERE p.key IN $keys
OPTIONAL MATCH (p)-[r]-(x)
RETURN p.key AS key, p.name AS name, collect(DISTINCT type(r)) AS rels
"""

MERGE_NODE_CYPHER = """
MERGE (target:Person {namespace: $ns, key: $target_key})
ON CREATE SET target.name = $name, target.first_seen = $now, target.previous_names = []
SET target.previous_names = reduce(acc = coalesce(target.previous_names, []), n IN $names |
        CASE WHEN n = target.name OR n IN acc THEN acc ELSE acc + n END),
    target.name = $name,
    target:Person
RETURN target.key AS key
"""

MOVE_INVOLVES_CYPHER = """
MATCH (e:Event {namespace: $ns})-[r:INVOLVES]->(old:Person {namespace: $ns, key: $old_key})
MATCH (target:Person {namespace: $ns, key: $target_key})
MERGE (e)-[new:INVOLVES]->(target)
ON CREATE SET new.created_at = r.created_at
DELETE r
RETURN count(*)
"""

MOVE_MENTIONS_CYPHER = """
MATCH (e:Event {namespace: $ns})-[r:MENTIONS]->(old {namespace: $ns, key: $old_key})
MATCH (target {namespace: $ns, key: $target_key})
MERGE (e)-[new:MENTIONS]->(target)
ON CREATE SET new.created_at = r.created_at
DELETE r
RETURN count(*)
"""

MOVE_ALIAS_CYPHER = """
MATCH (old {namespace: $ns, key: $old_key})-[r:ALIAS_OF]->(a:Alias)
MATCH (target {namespace: $ns, key: $target_key})
MERGE (target)-[new:ALIAS_OF]->(a)
ON CREATE SET new.created_at = r.created_at, new.label = r.label,
              new.confidence = r.confidence, new.source = r.source,
              new.source_event_id = r.source_event_id
DELETE r
RETURN count(*)
"""

DELETE_OLD_CYPHER = """
MATCH (p:Person {namespace: $ns, key: $key})
DETACH DELETE p
RETURN count(*)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="把名字键 Person 合并到 uid 键（spec 045）")
    ap.add_argument("--apply", action="store_true", help="真的执行（默认 dry-run）")
    ap.add_argument("--backup", default="", help="导出受影响子图到 JSON")
    args = ap.parse_args()

    settings = load_settings()
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    total_groups = total_moved = total_deleted = 0
    backup: list[dict] = []
    try:
        with drv.session() as session:
            groups = list(session.run(GROUPS_CYPHER))
            print(f"Account 分组 {len(groups)} 个（有 nicks 的）")
            for g in groups:
                ns, uid, names = g["ns"], g["uid"], list(g["nicks"] or [])
                if not names:
                    continue
                cands = list(session.run(CANDIDATES_CYPHER, ns=ns, names=names))
                if not cands:
                    continue
                total_groups += 1
                target = f"uid:{uid}"
                display = cands[-1]["name"] if cands else names[-1]
                print(f"\n[{ns}] uid={uid} → {target}（显示名 {display}）")
                for c in cands:
                    print(f"   合并 {c['name']}({c['key']}) 边: "
                          f"INVOLVES={c['involves']} MENTIONS={c['mentions']} ALIAS={c['aliases']}")
                if args.backup:
                    backup.extend(dict(r) for r in session.run(
                        BACKUP_CYPHER, ns=ns, keys=[c["key"] for c in cands]))
                if not args.apply:
                    continue
                session.run(MERGE_NODE_CYPHER, ns=ns, target_key=target,
                            name=display, names=names + [c["name"] for c in cands],
                            now=__import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc).isoformat()).consume()
                for c in cands:
                    for cypher in (MOVE_INVOLVES_CYPHER, MOVE_MENTIONS_CYPHER, MOVE_ALIAS_CYPHER):
                        res = session.run(cypher, ns=ns, old_key=c["key"],
                                          target_key=target).single()
                        total_moved += int(res[0]) if res else 0
                    session.run(DELETE_OLD_CYPHER, ns=ns, key=c["key"]).consume()
                    total_deleted += 1

        if args.backup and backup:
            Path(args.backup).write_text(
                json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\n备份 {len(backup)} 条 → {args.backup}")

        print(f"\n汇总：合并组 {total_groups} / 迁移边 {total_moved} / 删除旧节点 {total_deleted}")
        if not args.apply:
            print("（dry-run；加 --apply 才执行）")
        return 0
    finally:
        drv.close()


if __name__ == "__main__":
    sys.exit(main())
