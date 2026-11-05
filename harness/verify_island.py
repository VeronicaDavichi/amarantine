"""真库验证：孤岛逻辑在现有 3 条数据上会怎么判？

期望：
  ① 默认（关闭）→ 不判孤岛
  ② 开启但数据量门槛 50 → 仍然不判（3 < 50）
  ③ 门槛降到 1 → 判孤岛（证明逻辑本身在工作，不是接线断了）

注意：卡片必须是「老的 + 久未被召回」，否则会被宽限期保护掉 ——
宽限期在孤岛判定之前，这是正确行为，不是 bug。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from memory.config import load_settings
from memory.core.forget_policy import ForgetCandidate, classify
from memory.services.graph_search import event_neighborhood

NS = "work"
TODAY = date(2026, 11, 5)


def load_cards(settings):
    q = """
    { Get { MemoryEvent(limit: 200, where: {path: ["namespace"], operator: Equal, valueText: "%s"}) {
        event_id summary is_deleted } } }
    """ % NS
    body = httpx.post(
        f"{settings.WEAVIATE_URL}/v1/graphql", json={"query": q}, timeout=30.0
    ).json()
    hits = ((body.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
    return [h for h in hits if not h.get("is_deleted")]


def main() -> None:
    s = load_settings()
    cards = load_cards(s)
    ids = [h["event_id"] for h in cards]
    print(f"存活卡片 {len(cards)} 张\n")

    got = event_neighborhood(ids, NS, settings=s)
    if got is None:
        print("图不可用 → 按设计不判孤岛")
        return

    print("event_neighborhood:")
    print(f"  neighbors    = {got.neighbors}")
    print(f"  total_events = {got.total_events}")
    print()

    def judge(label: str, island_max: int, island_min: int) -> int:
        rows = [
            ForgetCandidate(
                event_id=h["event_id"],
                # 必须给真实的旧日期：created_at=None 会让 age=0，
                # 卡被宽限期保护掉，根本走不到孤岛判定。
                created_at=TODAY - timedelta(days=200),
                last_recalled_at=TODAY - timedelta(days=45),
                recall_count=0,
                retention="normal",
                neighbor_events=got.neighbors.get(h["event_id"], 0),
            )
            for h in cards
        ]
        verdicts = [
            classify(
                c,
                today=TODAY,
                idle_days=30,
                min_recalls=2,
                grace_days=7,
                protect_importance=0.9,
                island_max_neighbors=island_max,
                island_min_events=island_min,
                namespace_events=got.total_events,
            )
            for c in rows
        ]
        islands = sum(1 for v in verdicts if v.reason == "island_and_unrecalled")
        print(label)
        print(f"  reasons  = {sorted({v.reason for v in verdicts})}")
        print(f"  判为孤岛 = {islands} / {len(verdicts)}")
        print()
        return islands

    n1 = judge("① 默认关闭（island_max=0）", 0, 50)
    n2 = judge("② 开启但门槛 50（数据只有 3 条）", 2, 50)
    n3 = judge("③ 开启且门槛降到 1（证明逻辑在工作）", 2, 1)

    print("=== 结论 ===")
    if n1 == 0 and n2 == 0 and n3 == len(cards):
        print(f"  ✓ 默认关闭生效（① 判 0），数据量门槛生效（② 判 0），")
        print(f"    逻辑本身工作（③ 判 {n3}/{len(cards)}）")
    else:
        print(f"  ✗ 异常：① {n1} / ② {n2} / ③ {n3}")


if __name__ == "__main__":
    main()
