"""清理 work 命名空间的脏数据，并重建 3 张真实卡。

默认 dry-run，什么都不改。

删除边界（spec 021 D1）：
    事件卡（Weaviate）→ 软删（D7/A9 事实层永不物理删）
    图节点（Neo4j）  → DETACH DELETE（D2 图可丢弃重建，属索引层）

**为什么 3 张真实卡也要软删**：
去重是按 `event_id` 查的（`get_by_event_id`）。重建后 `entity_names` 变了
→ `event_id` 变了 → 查不到旧卡 → **新建一条**。旧卡留着就变成 6 张重复。
所以软删旧卡（留痕可审计）+ 写新卡，才是「重建」的正确语义。

分两步执行，中间可以检查：
    ./harness/dev-linux.sh python ../harness/purge_test_data.py             # dry-run
    ./harness/dev-linux.sh python ../harness/purge_test_data.py --purge     # 只清理
    ./harness/dev-linux.sh python ../harness/purge_test_data.py --rebuild   # 只重建
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

import httpx
from neo4j import GraphDatabase

from memory.config import load_settings

NS = "work"

#: spec 021 D2：只认显式标记，不做启发式猜测
TEST_MARKERS = ("e2e-", "冒烟测试", "迁移验证", "验证标记")

#: 要重建的真实内容（spec 021 D4）。
#: 注意：`remember_event` 没有 types 参数 —— 打标由内部打标器重算，
#: 结果可能和旧卡不同。这是「重新处理」而不是「恢复」，见 spec 021 D4。
REBUILD = [
    "张三决定项目A延期到六月",
    "李四确定了新项目B的启动时间",
    "周会上讨论了明年的预算分配方案",
]


@dataclass
class Card:
    event_id: str
    summary: str

    @property
    def is_test(self) -> bool:
        return any(m in self.summary for m in TEST_MARKERS)


def load_cards(settings) -> list[Card]:
    query = """
    { Get { MemoryEvent(limit: 500, where: {path: ["namespace"], operator: Equal, valueText: "%s"}) {
        event_id summary } } }
    """ % NS
    body = httpx.post(
        f"{settings.WEAVIATE_URL}/v1/graphql", json={"query": query}, timeout=30.0
    ).json()
    if body.get("errors"):
        raise SystemExit(f"Weaviate 查询失败: {body['errors'][:1]}")
    hits = ((body.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
    return [
        Card(event_id=str(h.get("event_id") or ""), summary=str(h.get("summary") or ""))
        for h in hits
    ]


def load_graph_events(settings) -> dict[str, int]:
    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    try:
        with drv.session() as session:
            return {
                str(r["eid"]): int(r["degree"])
                for r in session.run(
                    """
                    MATCH (e:Event {namespace: $ns})
                    OPTIONAL MATCH (e)-[r]-()
                    RETURN e.event_id AS eid, count(r) AS degree
                    """,
                    ns=NS,
                )
            }
    finally:
        drv.close()


def report(cards: list[Card], graph: dict[str, int]) -> None:
    test_cards = [c for c in cards if c.is_test]
    real_cards = [c for c in cards if not c.is_test]
    card_ids = {c.event_id for c in cards}
    orphans = [eid for eid in graph if eid not in card_ids]

    print(f"=== 清理计划 · namespace={NS} ===\n")
    print(f"卡片总数: {len(cards)}")
    print(f"  测试残留 → 软删: {len(test_cards)}")
    print(f"  真实内容 → 软删后重建: {len(real_cards)}")
    print(f"\n图节点: {len(graph)}  （要删 {len(cards) + len(orphans)} 个：全部卡片 + 孤儿）")
    print(f"  孤儿节点（图有卡无）: {len(orphans)}")

    print(f"\n--- 真实内容卡（软删后重建）---")
    for c in real_cards:
        print(f"  {c.event_id}  {c.summary[:44]}")

    print(f"\n--- 孤儿图节点（前 6 个）---")
    for eid in orphans[:6]:
        print(f"  {eid}  degree={graph[eid]}")

    print(f"\n--- 重建清单 ---")
    for s in REBUILD:
        print(f"  {s}")


def do_purge(settings, cards: list[Card], graph: dict[str, int]) -> None:
    from memory.services.forget import forget_event

    card_ids = {c.event_id for c in cards}
    orphans = [eid for eid in graph if eid not in card_ids]

    print("\n=== ① 软删全部卡片（事实层留痕）===")
    ok = err = 0
    for c in cards:
        try:
            forget_event(
                namespace=NS, event_id=c.event_id, reason="outdated", settings=settings
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞整体
            err += 1
            print(f"  失败 {c.event_id}: {exc}")
    print(f"  软删成功 {ok}，失败 {err}")

    print("\n=== ② 物理删图节点（索引层，D2 允许）===")
    delete_ids = [c.event_id for c in cards] + orphans
    drv = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    try:
        with drv.session() as session:
            result = session.run(
                """
                UNWIND $ids AS eid
                MATCH (e:Event {namespace: $ns, event_id: eid})
                DETACH DELETE e
                RETURN count(e) AS deleted
                """,
                ids=delete_ids,
                ns=NS,
            ).single()
            print(f"  删除图节点 {result['deleted'] if result else 0} 个")
    finally:
        drv.close()


def do_rebuild(settings) -> None:
    from memory.services.remember import remember_event

    print("\n=== ③ 重建真实卡 ===")
    rebuilt = []
    for summary in REBUILD:
        try:
            got = remember_event(namespace=NS, summary=summary, settings=settings)
            eid = getattr(got, "event_id", None)
            rebuilt.append({"summary": summary, "event_id": eid})
            print(f"  ok {eid}: {summary}")
        except Exception as exc:  # noqa: BLE001
            print(f"  失败 {summary}: {exc}")
    print("\n=== 重建结果 ===")
    print(json.dumps(rebuilt, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge", action="store_true", help="软删卡片 + 删图节点")
    ap.add_argument("--rebuild", action="store_true", help="重建 3 张真实卡")
    args = ap.parse_args()

    settings = load_settings()
    cards = load_cards(settings)
    graph = load_graph_events(settings)
    report(cards, graph)

    if not (args.purge or args.rebuild):
        print("\n[dry-run] 什么都没改。用 --purge / --rebuild 执行。")
        return

    if args.purge:
        do_purge(settings, cards, graph)
    if args.rebuild:
        do_rebuild(settings)


if __name__ == "__main__":
    main()
