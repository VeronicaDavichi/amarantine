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
from memory.services.labeler import extract_entities

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

    # 「实体名为空」必须分成两种，混在一起就是假警报（spec 030）：
    #   alone —— 这句话本来就抽不出实体（"周会上讨论了预算"，没有名字）
    #   lost  —— 现在能抽出来，但卡里是空的（写入链路丢了，真出问题）
    # 用同一条规则复算，因为写入和查询共用 extract_entities 才是设计意图。
    alone, lost = [], []
    for c in cards:
        if c.get("entity_names"):
            continue
        summary = str(c.get("summary") or "")
        try:
            recomputed = extract_entities(summary) if summary else []
        except Exception:  # noqa: BLE001 - 诊断脚本不该因为一句解析失败就崩
            recomputed = []
        (lost if recomputed else alone).append(c)

    print(f"  实体名为空: {len(alone) + len(lost)}")
    if alone:
        print(f"    其中 {len(alone)} 张本来就无可提取实体（正常）")
    if lost:
        print(f"    ⚠️ 其中 {len(lost)} 张本该有实体但现在没有（写入链路丢了）")
        for c in lost[:3]:
            print(f"      {str(c.get('summary') or '')[:36]}")

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
                # 「从未生效」这种措辞把两件事混在一起了（spec 030）：
                #   逻辑可能错了，或者数据量根本不够。
                # 前者是真 bug，后者是预期 —— 必须让人一眼分清。
                gap = s.GRAPH_HUB_DEGREE - max(degrees)
                print(
                    f"  hub 保护：最大度数 {max(degrees)}，阈值 "
                    f"{s.GRAPH_HUB_DEGREE}，差 {gap}"
                )
                if gap <= 3:
                    print("    → 快到了，数据再长一点就能触发")
                else:
                    print(
                        "    → 当前规模下结构上不可达，属预期"
                        "（典型事件 4-6 条边，阈值 8 是「内容特别丰富」）"
                    )
                    print(
                        "    → 置信度：Cypher 和端到端链路已由 "
                        "tests/test_hub_end_to_end.py 在真图上验证"
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
