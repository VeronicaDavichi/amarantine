#!/usr/bin/env python3
"""账号族构建（spec 042）：词形族 blocking → 候选边。

用法：

    python ../harness/build_account_families.py --namespace njzq-dingtalk
    python ../harness/build_account_families.py --namespace njzq-dingtalk --apply

产出：

- 同名不同 uid → `(:Account)-[:SAME_AS {label:"probable"}]->(:Account)`
- `xx的<主体>` → `(:Account)-[:INSTANCE_OF {label:"uncertain"}]->(:Bot {name:<主体>})`

**只加边，不并节点**：账号有 uid 是硬事实，主体是推断。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.account_family import (  # noqa: E402
    AccountRecord,
    MERGE_INSTANCE_CYPHER,
    MERGE_SAME_AS_CYPHER,
    find_families,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="账号族候选（spec 042）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()

    from memory.config import load_settings
    from neo4j import GraphDatabase

    settings = load_settings()
    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    now = datetime.now(timezone.utc).isoformat()
    try:
        with drv.session() as session:
            accounts = [
                AccountRecord(uid=r["uid"], nick=r["nick"] or "", namespace=args.namespace)
                for r in session.run(
                    "MATCH (a:Account {namespace:$ns}) RETURN a.uid AS uid, a.nick AS nick",
                    ns=args.namespace,
                )
            ]
            print(f"账号 {len(accounts)} 个")
            fams = find_families(accounts)
            print(f"候选族 {len(fams)} 个")
            for f in fams:
                print(f"  [{f.label:>9}] {f.kind:<9} {f.family:<12} "
                      f"成员 {len(f.members)}：{list(f.members)[:5]}")

            if not args.apply:
                print("\n（dry-run；加 --apply 才写边）")
                return 0

            same_as = [
                {"a": m[0], "b": m[1], "label": "probable", "confidence": 0.7,
                 "evidence": []}
                for f in fams if f.kind == "same_nick"
                for m in [sorted(f.members)]
            ]
            instance = [
                {"uid": uid, "bot": f.family, "label": "uncertain", "confidence": 0.5}
                for f in fams if f.kind == "suffix"
                for uid in f.members
            ]

            written = 0
            if same_as:
                res = session.run(MERGE_SAME_AS_CYPHER, namespace=args.namespace,
                                  items=same_as, now=now).single()
                n = int(res["n"]) if res else 0
                written += n
                print(f"  写入 SAME_AS: {n}")
            if instance:
                res = session.run(MERGE_INSTANCE_CYPHER, namespace=args.namespace,
                                  items=instance, now=now).single()
                n = int(res["n"]) if res else 0
                written += n
                print(f"  写入 INSTANCE_OF: {n}")
            print(f"\n共写入 {written} 条边")
            return 0
    finally:
        drv.close()


if __name__ == "__main__":
    sys.exit(main())
