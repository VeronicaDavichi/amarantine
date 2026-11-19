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
    extract_statements_combined,
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
#: 关系两端节点的统一寻址：`(kind, key)`。
#: - person / entity：按 `name` 匹配对应标签
#: - account：按 `uid` 匹配（Account 没有 name，名字在 Alias 上）
_MATCH_END = """
MATCH ({var}) WHERE {var}.namespace = $namespace AND (
  ({var}.kind_hint = 'Account' AND {var}.uid = {key})
  OR ({var}.kind_hint <> 'Account' AND {var}.name = {key}
      AND {var}.kind_hint IN labels({var}))
)"""


def _end_match(var: str, side: str) -> str:
    """按 `(kind, key)` 精确定位节点。

    Person / Entity 都带 `key` 属性；Account 只有 `uid`。
    用 key 而不是 name 匹配：同名不同类型节点（`豆包` 既是 Entity 又是 Person）
    会让 name 匹配产生重复行。
    """
    return (
        f"MATCH ({var}) WHERE {var}.namespace = $namespace AND (\n"
        f"  (item.{side}_kind = 'Account' AND {var}.uid = item.{side}_key)\n"
        f"  OR (item.{side}_kind <> 'Account' AND {var}.key = item.{side}_key\n"
        f"      AND item.{side}_kind IN labels({var}))\n"
        f")"
    )


def write_cypher(predicate: str) -> str:
    """生成一条谓词的写边 Cypher（两端都按 kind+key 解析）。"""
    return f"""
UNWIND $items AS item
{_end_match("h", "head")}
WITH DISTINCT item, h
{_end_match("t", "tail")}
WITH DISTINCT item, h, t
MERGE (h)-[r:{predicate}]->(t)
ON CREATE SET r.first_seen = $now, r.evidence = [], r.source = item.source
SET r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    r.last_seen = $now
SET r.evidence_count = size(r.evidence),
    r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = CASE WHEN r.label = 'exact' THEN 1.0 ELSE item.confidence END,
    r.source = CASE WHEN r.source = 'statement' THEN 'statement' ELSE item.source END
RETURN count(r) AS n
"""


#: 缺失的尾实体补一个 Entity 节点（`kind=topic`，`source` 可追溯）
ENSURE_ENTITY_CYPHER = """
UNWIND $names AS name
MERGE (e:Entity {namespace: $namespace, name: name})
ON CREATE SET e.kind = 'topic', e.key = 'ent_' + name,
              e.created_at = $now, e.source = 'relation_stmt'
RETURN count(e) AS n
"""


#: 名字能解析到 Account 但没有 Person 时，建一个**账号锚定**的 Person
#: （spec 045：Person 的键用 uid，不随名字变），并把 Account 挂上去。
ENSURE_PERSON_CYPHER = """
UNWIND $rows AS row
MATCH (a:Account {namespace: $namespace, uid: row.uid})
MERGE (p:Person {namespace: $namespace, key: 'acct_' + row.uid})
ON CREATE SET p.name = row.name, p.created_at = $now,
              p.source = 'relation_stmt'
MERGE (a)-[r:OPERATED_BY]->(p)
ON CREATE SET r.created_at = $now
RETURN count(p) AS n
"""


def resolve_ends(session, namespace: str, names: set[str]):
    """把名字解析成 `(标签, key)`，缺 Person 的按账号锚定补建。

    优先级：Person > Entity > Account(经 Alias，补建 Person) > 新建 Entity。

    Returns:
        `(resolved, missing, anchored)`；`anchored` 是补建 Person 的条数。
    """
    people = {r["name"]: r["key"] for r in session.run(
        "MATCH (p:Person {namespace:$ns}) RETURN p.name AS name, p.key AS key",
        ns=namespace) if r.get("name") and r.get("key")}
    entities = {r["name"]: r["key"] for r in session.run(
        "MATCH (e:Entity {namespace:$ns}) RETURN e.name AS name, e.key AS key",
        ns=namespace) if r.get("name") and r.get("key")}
    accounts = {}
    for rec in session.run(
        "MATCH (a:Account {namespace:$ns})-[:DISPLAYED_AS]->(al:Alias {namespace:$ns}) "
        "RETURN al.name AS name, a.uid AS uid", ns=namespace):
        accounts.setdefault(rec["name"], rec["uid"])

    resolved: dict[str, tuple[str, str]] = {}
    to_anchor: list[dict] = []
    missing: list[str] = []
    for name in names:
        if not name:
            continue
        if name in people:
            resolved[name] = ("Person", people[name])
        elif name in entities:
            resolved[name] = ("Entity", entities[name])
        elif name in accounts:
            uid = accounts[name]
            to_anchor.append({"name": name, "uid": uid})
            resolved[name] = ("Person", f"acct_{uid}")
        else:
            missing.append(name)
    return resolved, missing, to_anchor


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
    ap.add_argument(
        "--use-llm", action="store_true",
        help="用 LLM 抽陈述（改造 A：规则 exact + LLM probable + 实体约束）",
    )
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--statements", action="store_true", help="同时跑陈述通道")
    ap.add_argument(
        "--all-cards", action="store_true",
        help="对每张卡都调 LLM（默认只处理含关系词的卡，省 90%% 调用）",
    )
    ap.add_argument("--limit-cards", type=int, default=0,
                    help="只处理前 N 张含关系词的卡（调试用）")
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
                # 名册：图里已有的 Person 名字，补 jieba 认不出的群友
                roster = [r["name"] for r in session.run(
                    "MATCH (p:Person {namespace: $namespace}) RETURN p.name AS name",
                    namespace=args.namespace) if r.get("name")]
                st: list = []
                rejected_all: list[dict] = []
                texts = [row.get("summary") or "" for row in summaries]
                if args.use_llm:
                    # **营销不做关系判定**（用户 2026-11-19）：
                    # 营销内容的用途只有「有没有我关注过的子项」，问「谁维护什么」
                    # 没有意义。卡摘要把链接与图片都概括掉了，所以要按**卡片业务词**
                    # 判，不能用原始消息那套（URL/图片行数恒为 0）。
                    from memory.services.marketing import looks_like_marketing_card

                    before = len(texts)
                    skipped_mk = [t for t in texts if looks_like_marketing_card(t)[0]]
                    texts = [t for t in texts if not looks_like_marketing_card(t)[0]]
                    print(f"  跳过营销卡 {len(skipped_mk)} 张（营销不做关系判定）")
                    for t in skipped_mk[:3]:
                        print(f"    · {t[:50]}")
                if args.use_llm and not args.all_cards:
                    # 只喂含关系词的卡：全量 2546 张逐张调 LLM 要数小时，
                    # 而实测只有约 10% 的卡里有关系动词（其余是「发了图片」）。
                    from memory.services.relations import RELATION_CUES

                    texts = [t for t in texts if any(c in t for c in RELATION_CUES)]
                    if args.limit_cards:
                        texts = texts[: args.limit_cards]
                    print(f"  含关系词的卡 {len(texts)} 张（--all-cards 可全扫）")
                for i, summary in enumerate(texts):
                    # 上下文 = 前 2 条摘要（理解指代用；不从里面抽关系）
                    ctx = [t for t in texts[max(0, i - 2): i] if t]
                    claims_i, rejected = extract_statements_combined(
                        summary, settings, context=ctx, roster=roster,
                        use_llm=args.use_llm)
                    rejected_all.extend(rejected)
                    for claim in claims_i:
                        st.append(claim)
                        print(f"  陈述[{claim.source}]: {claim.head} "
                              f"-[{claim.predicate}]-> {claim.tail}")
                    if args.use_llm and (i + 1) % 20 == 0:
                        print(f"    进度 {i + 1}/{len(texts)}")
                print(f"陈述通道 → {len(st)} 条边（扫 {len(summaries)} 张卡，"
                      f"LLM={'开' if args.use_llm else '关'}）")
                if args.use_llm:
                    print(f"  实体约束拒绝 {len(rejected_all)} 条")
                    for r in rejected_all[:10]:
                        print(f"    ✗ {r.get('head')} → {r.get('tail')} "
                              f"({r.get('reason')})")
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
            # 两端节点先解析：缺失的建 Entity，否则 MATCH 不中、边全丢
            # （实测：65 条 claim 只写出 2 条，因为 `商品`/`服务器` 没有节点）
            names: set[str] = set()
            for items in by_predicate.values():
                for it in items:
                    names.add(it["head"])
                    names.add(it["tail"])
            resolved, missing, to_anchor = resolve_ends(session, args.namespace, names)
            if to_anchor:
                session.run(ENSURE_PERSON_CYPHER, namespace=args.namespace,
                            rows=to_anchor, now=now)
                print(f"补建账号锚定 Person {len({r['uid'] for r in to_anchor})} 个")
            if missing:
                print(f"\n补建 Entity 节点 {len(missing)} 个：{sorted(missing)[:8]}…")
                session.run(ENSURE_ENTITY_CYPHER, namespace=args.namespace,
                            names=sorted(missing), now=now)
                for name in missing:
                    resolved[name] = ("Entity", f"ent_{name}")

            unresolved = 0
            for predicate, items in by_predicate.items():
                payload = []
                for it in items:
                    hk = resolved.get(it["head"])
                    tk = resolved.get(it["tail"])
                    if not hk or not tk:
                        unresolved += 1
                        continue
                    payload.append({**it, "head_kind": hk[0], "head_key": hk[1],
                                    "tail_kind": tk[0], "tail_key": tk[1]})
                if not payload:
                    continue
                res = session.run(write_cypher(predicate), namespace=args.namespace,
                                  items=payload, now=now).single()
                n = int(res["n"]) if res else 0
                written += n
                print(f"  写入 {predicate}: {n} / {len(payload)}")
            if unresolved:
                print(f"  ⚠ 未解析 {unresolved} 条（节点缺失）")
            print(f"\n共写入 {written} 条边")
            return 0
    finally:
        drv.close()


if __name__ == "__main__":
    sys.exit(main())
