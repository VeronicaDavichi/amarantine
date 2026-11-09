#!/usr/bin/env python3
"""spec 038 REQ-5 / A6：清理低置信的脏别名边。

`ALIAS_OF` 上带 `label` 与 `confidence`：

- ``exact``：用户亲口说的等价关系，**不动**
- ``probable``：规则层推出来的，**不动**
- ``uncertain``：置信度低于 `ALIAS_PROBABLE_MIN`，多半是摘要句式误抽
  （`任泽昂说明就是这个接口` → `接口 -> 任泽昂说明`）。默认只清理这一类。

用法（默认 dry-run，只看不删）：

    python ../harness/clean_alias_edges.py                # 列出候选
    python ../harness/clean_alias_edges.py --apply        # 真删
    python ../harness/clean_alias_edges.py --max-conf 0.5 # 改阈值
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402

LIST_CYPHER = """
MATCH (a:Alias)<-[r:ALIAS_OF]-(p)
WHERE coalesce(r.label, 'uncertain') = 'uncertain'
  AND coalesce(r.confidence, 0.0) <= $max_conf
RETURN a.name AS alias, p.name AS canonical,
       coalesce(r.confidence, 0.0) AS confidence,
       r.source_event_id AS event_id
ORDER BY confidence
"""

DELETE_CYPHER = """
MATCH (a:Alias)<-[r:ALIAS_OF]-(p)
WHERE coalesce(r.label, 'uncertain') = 'uncertain'
  AND coalesce(r.confidence, 0.0) <= $max_conf
DELETE r
RETURN count(*) AS removed
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 uncertain 别名边（spec 038 A6）")
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只列）")
    ap.add_argument("--max-conf", type=float, default=0.5, help="置信度上限，默认 0.5")
    args = ap.parse_args()

    settings = load_settings()
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    try:
        with drv.session() as session:
            rows = list(session.run(LIST_CYPHER, max_conf=args.max_conf))
            print(f"候选 {len(rows)} 条（uncertain 且 confidence <= {args.max_conf}）：")
            for r in rows:
                print(
                    f"  {r['alias']} -> {r['canonical']}"
                    f"  conf={r['confidence']}  event={r['event_id']}"
                )
            if not rows:
                print("没有需要清理的边 ✓")
                return 0
            if not args.apply:
                print("\n（dry-run；加 --apply 才真删）")
                return 0
            removed = session.run(DELETE_CYPHER, max_conf=args.max_conf).single()
            print(f"\n已删除 {removed['removed']} 条 uncertain 边")
            left = list(session.run(LIST_CYPHER, max_conf=args.max_conf))
            print(f"复检：剩余 {len(left)} 条")
            return 0 if not left else 1
    finally:
        drv.close()


if __name__ == "__main__":
    sys.exit(main())
