#!/usr/bin/env python3
"""关系谓词落库（spec 039）：陈述 + 行为双通道 → 图上带证据的边。

用法：

    # 只看会建哪些边（默认）
    python ../harness/build_relations.py --namespace njzq-dingtalk

    # 真写
    python ../harness/build_relations.py --namespace njzq-dingtalk --apply

    # 调阈值 / 行为窗口
    python ../harness/build_relations.py --threshold 3 --window-days 30 --apply

## 两条通道

- **行为**：`(Person)<-[:INVOLVES]-(Event)-[:ACTION]->(Action)` 聚合，
  目标取 `Event-[:MENTIONS]->(Entity)`；同 (人, 谓词, 目标) ≥ 阈值才建边。
- **陈述**：扫事件卡摘要，用规则句式抽 `X 负责 Y` 一类声明（`label=exact`）。

边上的属性：`label`（exact/probable）、`evidence`（event_id 数组）、
`evidence_count`、`source`、`first_seen`/`last_seen`。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.relations import (  # noqa: E402
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    aggregate_behaviors,
    extract_statements,
    merge_claims,
)

BEHAVIOR_CYPHER = """
MATCH (p:Person {namespace: $namespace})<-[:INVOLVES]-(e:Event)-[:ACTION]->(a:Action)
MATCH (e)-[:MENTIONS]->(m)
RETURN p.name AS person, a.verb AS verb, m.name AS target,
       e.event_id AS event_id, e.date_start AS date
"""

SUMMARIES_CYPHER = """
MATCH (e:Event {namespace: $namespace})
RETURN e.event_id AS event_id, e.summary AS summary
"""

#: 每种谓词一条，因为关系类型不能参数化。
WRITE_CYPHER = {
    "OWNS": """
UNWIND $items AS item
MATCH (h {namespace: $namespace, name: item.head})
MATCH (t {namespace: $namespace, name: item.tail})
MERGE (h)-[r:OWNS]->(t)
ON CREATE SET r.first_seen = $now, r.evidence = [], r.source = item.source
SET r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    r.last_seen = $now
// 计数必须**在 evidence 更新之后**单独 SET：同一条 SET 里 size(r.evidence)
// 读到的是旧值（实测写成 0），会被算成「没有证据」。
SET r.evidence_count = size(r.evidence),
    r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = CASE WHEN r.label = 'exact' THEN 1.0 ELSE item.confidence END,
    r.source = CASE WHEN r.source = 'statement' THEN 'statement' ELSE item.source END
RETURN count(r) AS n
""",
    "MAINTAINS": """
UNWIND $items AS item
MATCH (h {namespace: $namespace, name: item.head})
MATCH (t {namespace: $namespace, name: item.tail})
MERGE (h)-[r:MAINTAINS]->(t)
ON CREATE SET r.first_seen = $now, r.evidence = [], r.source = item.source
SET r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    r.last_seen = $now
// 计数必须**在 evidence 更新之后**单独 SET：同一条 SET 里 size(r.evidence)
// 读到的是旧值（实测写成 0），会被算成「没有证据」。
SET r.evidence_count = size(r.evidence),
    r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = CASE WHEN r.label = 'exact' THEN 1.0 ELSE item.confidence END,
    r.source = CASE WHEN r.source = 'statement' THEN 'statement' ELSE item.source END
RETURN count(r) AS n
""",
    "COLLABORATES_WITH": """
UNWIND $items AS item
MATCH (h:Person {namespace: $namespace, name: item.head})
MATCH (t:Person {namespace: $namespace, name: item.tail})
MERGE (h)-[r:COLLABORATES_WITH]->(t)
ON CREATE SET r.first_seen = $now, r.evidence = [], r.source = item.source
SET r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    r.last_seen = $now
// 计数必须**在 evidence 更新之后**单独 SET：同一条 SET 里 size(r.evidence)
// 读到的是旧值（实测写成 0），会被算成「没有证据」。
SET r.evidence_count = size(r.evidence),
    r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = CASE WHEN r.label = 'exact' THEN 1.0 ELSE item.confidence END,
    r.source = CASE WHEN r.source = 'statement' THEN 'statement' ELSE item.source END
RETURN count(r) AS n
""",
    "DEPENDS_ON": """
UNWIND $items AS item
MATCH (h {namespace: $namespace, name: item.head})
MATCH (t {namespace: $namespace, name: item.tail})
MERGE (h)-[r:DEPENDS_ON]->(t)
ON CREATE SET r.first_seen = $now, r.evidence = [], r.source = item.source
SET r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    r.last_seen = $now
// 计数必须**在 evidence 更新之后**单独 SET：同一条 SET 里 size(r.evidence)
// 读到的是旧值（实测写成 0），会被算成「没有证据」。
SET r.evidence_count = size(r.evidence),
    r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = CASE WHEN r.label = 'exact' THEN 1.0 ELSE item.confidence END,
    r.source = CASE WHEN r.source = 'statement' THEN 'statement' ELSE item.source END
RETURN count(r) AS n
""",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="关系谓词落库（spec 039）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--statements", action="store_true", help="同时跑陈述通道")
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
            rows = [dict(r) for r in session.run(BEHAVIOR_CYPHER, namespace=args.namespace)]
            print(f"行为行 {len(rows)} 条（人-动作-对象）")
            claims = aggregate_behaviors(
                rows, threshold=args.threshold, window_days=args.window_days
            )
            print(f"行为通道 → {len(claims)} 条边（阈值 {args.threshold}，窗口 {args.window_days} 天）")

            if args.statements:
                summaries = [dict(r) for r in session.run(SUMMARIES_CYPHER, namespace=args.namespace)]
                st: list = []
                for row in summaries:
                    st.extend(extract_statements(row.get("summary") or ""))
                print(f"陈述通道 → {len(st)} 条边（扫 {len(summaries)} 张卡）")
                claims = merge_claims(claims + st)

            for c in claims[:20]:
                print(f"  {c.head} -[{c.predicate}]-> {c.tail}  "
                      f"label={c.label} evidence={len(c.evidence)}")
            if len(claims) > 20:
                print(f"  …还有 {len(claims) - 20} 条")

            if not args.apply:
                print("\n（dry-run；加 --apply 才写）")
                return 0

            written = 0
            by_predicate: dict[str, list[dict]] = {}
            for c in claims:
                by_predicate.setdefault(c.predicate, []).append({
                    "head": c.head, "tail": c.tail, "label": c.label,
                    "confidence": c.confidence, "evidence": list(c.evidence),
                    "source": c.source,
                })
            for predicate, items in by_predicate.items():
                cypher = WRITE_CYPHER.get(predicate)
                if not cypher:
                    print(f"⚠ 未知谓词 {predicate}，跳过")
                    continue
                res = session.run(cypher, namespace=args.namespace, items=items, now=now).single()
                n = int(res["n"]) if res else 0
                written += n
                print(f"  写入 {predicate}: {n}")
            print(f"\n共写入 {written} 条边")
            return 0
    finally:
        drv.close()


if __name__ == "__main__":
    sys.exit(main())
