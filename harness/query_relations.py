#!/usr/bin/env python3
"""关系查询（spec 039 A7）：按名字查 2 跳内的关系路径，支持三档。

用法：

    python ../harness/query_relations.py --namespace njzq-dingtalk --name 王泳
    python ../harness/query_relations.py --namespace njzq-dingtalk --name 王泳 --tier explore
    python ../harness/query_relations.py --namespace njzq-dingtalk --name 王泳 --depth 2

输出的每一行是一条**有向路径**（`A -[谓词]-> B`），带 `label` 与证据数。
只走 039 定义的四个谓词，不碰 `RELATED` 这类挂载边 —— 否则 2 跳会炸成几千条。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

#: 039 定义的谓词（与 relations.PREDICATE_SPECS 保持一致）
PREDICATES = ("OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON")


def main() -> int:
    ap = argparse.ArgumentParser(description="关系路径查询（spec 039）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--name", required=True, help="起点名字（人/实体）")
    ap.add_argument("--tier", default="default", choices=("strict", "default", "explore"))
    ap.add_argument("--depth", type=int, default=2, choices=(1, 2))
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    from memory.config import load_settings
    from memory.services.reference import TIER_STATUSES
    from neo4j import GraphDatabase

    labels = list(TIER_STATUSES[args.tier])
    settings = load_settings()
    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    cypher = (
        f"MATCH p = (n {{namespace:$ns, name:$name}})-[*1..{args.depth}]->(m) "
        f"WHERE all(r IN relationships(p) WHERE type(r) IN $preds) "
        f"RETURN [x IN nodes(p) | x.name] AS names, "
        f"       [r IN relationships(p) | type(r)] AS preds, "
        f"       [r IN relationships(p) | coalesce(r.label,'uncertain')] AS labels, "
        f"       [r IN relationships(p) | size(coalesce(r.evidence, []))] AS evidence "
        f"LIMIT $limit"
    )
    try:
        with drv.session() as session:
            rows = list(session.run(cypher, ns=args.namespace, name=args.name,
                                    preds=list(PREDICATES), limit=args.limit))
    finally:
        drv.close()

    kept = [
        r for r in rows
        if all(label in labels for label in r["labels"])
    ]
    print(f"起点 {args.name} | tier={args.tier} | 路径 {len(kept)}/{len(rows)} 条")
    for r in kept:
        names, preds = r["names"], r["preds"]
        parts = [names[0]]
        for pred, nxt in zip(preds, names[1:]):
            parts.append(f"-[{pred}]-> {nxt}")
        print("  " + " ".join(parts) + f"    evidence={r['evidence']}")
    if not kept and rows:
        print("  （有路径但都被 tier 过滤掉了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
