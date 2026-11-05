"""记忆数据健康度检查。

起因：排查「孤岛降权」时发现 `work` 的 59 个事件节点度数全是 0 或 1，
追下去是三层问题（测试残留、实体名是垃圾、event_id 含实体名导致无法重建）。

以后想知道「图/数据健不健康」就跑这个，不用再手写一堆临时查询。

用法：
    ./harness/dev-linux.sh python ../harness/check_data_health.py [namespace]
"""

from __future__ import annotations

import sys
from collections import Counter

import httpx
from neo4j import GraphDatabase

from memory.config import load_settings

TEST_MARKERS = ("e2e-", "冒烟测试", "迁移验证", "验证标记")


def _weaviate_cards(settings, namespace: str) -> list[dict]:
    # **必须查 is_deleted 并在 Python 里过滤**：
    # 软删的卡仍留在 Weaviate（D7/A9 事实层永不物理删），不过滤会把
    # 「已遗忘」的卡算进健康度统计，得出「89% 是测试残留」这种假警报。
    query = """
    { Get { MemoryEvent(limit: 500, where: {path: ["namespace"], operator: Equal, valueText: "%s"}) {
        summary entity_names types is_deleted } } }
    """ % namespace
    body = httpx.post(
        f"{settings.WEAVIATE_URL}/v1/graphql", json={"query": query}, timeout=30.0
    ).json()
    if body.get("errors"):
        print(f"  Weaviate 查询报错: {body['errors'][:1]}")
        return []
    hits = ((body.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
    live = [h for h in hits if not h.get("is_deleted")]
    soft_deleted = len(hits) - len(live)
    if soft_deleted:
        print(f"  （另有 {soft_deleted} 张已软删，不计入健康度）")
    return live


def main() -> None:
    ns = sys.argv[1] if len(sys.argv) > 1 else "work"
    s = load_settings()

    print(f"=== 记忆数据健康度 · namespace={ns} ===\n")

    # 1) 卡片层
    cards = _weaviate_cards(s, ns)
    print(f"卡片数: {len(cards)}")
    residue = [c for c in cards if any(m in str(c.get("summary") or "") for m in TEST_MARKERS)]
    print(f"  测试残留: {len(residue)} ({len(residue) / max(1, len(cards)) * 100:.0f}%)")

    suspicious = [
        c for c in cards
        if any(len(str(e)) > 6 for e in (c.get("entity_names") or []))
    ]
    print(f"  实体名异常长（>6 字，疑似句子片段）: {len(suspicious)}")
    empty_ents = sum(1 for c in cards if not (c.get("entity_names") or []))
    print(f"  实体名为空: {empty_ents}")

    # 2) 图层
    drv = GraphDatabase.driver(s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD))
    try:
        with drv.session() as session:
            rows = list(
                session.run(
                    """
                    MATCH (e:Event {namespace: $ns})
                    OPTIONAL MATCH (e)-[r]-(n)
                    RETURN e.event_id AS eid, count(r) AS degree, collect(DISTINCT type(r)) AS rels
                    """,
                    ns=ns,
                )
            )
            if not rows:
                print("\n图: 该 namespace 没有 Event 节点")
                return

            degrees = [int(r["degree"]) for r in rows]
            dist = Counter(degrees)
            print(f"\n图节点: {len(rows)} 个 Event")
            print(f"  度数分布: {dict(sorted(dist.items()))}")
            print(f"  平均度数: {sum(degrees) / len(degrees):.2f}  最大: {max(degrees)}")

            all_rels: Counter = Counter()
            for r in rows:
                all_rels.update(r["rels"] or [])
            print(f"  边类型: {dict(all_rels)}")

            has_entity_edge = sum(
                1 for r in rows
                if set(r["rels"] or []) & {"MENTIONS", "INVOLVES"}
            )
            print(f"  有实体边的事件: {has_entity_edge} / {len(rows)}")

            # 3) 判定
            print("\n=== 判定 ===")
            if len(rows) and max(degrees) < s.GRAPH_HUB_DEGREE:
                print(
                    f"  ⚠️ 没有任何节点达到 GRAPH_HUB_DEGREE={s.GRAPH_HUB_DEGREE} —— "
                    "hub 保护从未生效"
                )
            if has_entity_edge == 0:
                print("  ⚠️ 一个实体边都没有 —— 图通道（实体扩展）对这批数据是失效的")
            if len(residue) > len(cards) * 0.5:
                print(f"  ⚠️ 测试残留占多数（{len(residue)}/{len(cards)}），图统计不可信")
            if not any([max(degrees) >= s.GRAPH_HUB_DEGREE, has_entity_edge, not residue]):
                print("  → 在这批数据上做图算法（孤岛/聚类/降权）测出来的都是垃圾的形状")
    finally:
        drv.close()


if __name__ == "__main__":
    main()
