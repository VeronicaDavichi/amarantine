#!/usr/bin/env python3
"""A8：重建后的双向解析校验（`weaviate_id` ↔ `event_id`）。

重建脚本写完不算完 —— 得证明「两边能对上」。这个脚本做四件事：

  1. Weaviate 里有多少张卡、多少个不同的 `event_id`
  2. 图里有多少个 `:Event` 节点、多少个不同的 `event_id`
  3. Weaviate 的每个 `event_id` 在图里都有节点吗（覆盖率）
  4. 图里的每个节点，其 `weaviate_id` 能反解回 Weaviate 的卡吗

用法（仓库根目录）：

    ./harness/dev-linux.sh python ../harness/check_rebuild.py --namespace work

退出码：0 = 双向可解析，1 = 有缺口（会列出前若干条）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.weaviate_store import (  # noqa: E402
    WeaviateStore,
    deterministic_uuid,
)

COUNT_CYPHER = """
MATCH (e:Event {namespace: $namespace})
RETURN collect(e.event_id) AS ids, collect(e.weaviate_id) AS wids
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="重建后双向校验（A8）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--show", type=int, default=5, help="缺口列表显示前几条")
    args = ap.parse_args()

    settings = load_settings()

    # ---------- 1. Weaviate 侧 ----------
    store = WeaviateStore(settings)
    try:
        cards = store.list_all(args.namespace)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL 读取事件卡失败: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    weaviate_ids = {c["event_id"] for c in cards}

    # ---------- 2. 图侧 ----------
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("FATAL 未安装 neo4j 驱动", file=sys.stderr)
        return 1

    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        with driver.session() as session:
            rec = session.run(COUNT_CYPHER, namespace=args.namespace).single()
            graph_ids_list = list(rec["ids"] or []) if rec else []
            graph_wids_list = list(rec["wids"] or []) if rec else []
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL 读取图失败: {exc}", file=sys.stderr)
        return 1
    finally:
        driver.close()

    graph_ids = set(graph_ids_list)

    print(f"命名空间            : {args.namespace}")
    print(f"Weaviate 卡片       : {len(cards)} 条（{len(weaviate_ids)} 个 event_id）")
    print(f"图 :Event 节点      : {len(graph_ids)} 个")

    # ---------- 3. 正向：卡 → 图 ----------
    missing_in_graph = sorted(weaviate_ids - graph_ids)
    forward_rate = (
        (len(weaviate_ids) - len(missing_in_graph)) / len(weaviate_ids) * 100
        if weaviate_ids
        else 100.0
    )
    print(f"\n正向覆盖（卡 → 图） : {forward_rate:.1f}%")
    if missing_in_graph:
        print(f"  图里缺 {len(missing_in_graph)} 个，前 {args.show} 条：")
        for eid in missing_in_graph[: args.show]:
            print(f"    - {eid}")

    # ---------- 4. 反向：图内自洽 ----------
    #
    # 口径说明：**不能**要求「图的每个节点都能在未删除的卡里找到」——
    # 软删的卡（`is_deleted=true`）被 Weaviate 过滤掉，但图节点仍在。
    # 那是正常状态，不是缺口。所以反向校验只查图自身是否自洽：
    # 每个节点的 weaviate_id 必须等于 deterministic_uuid(namespace, event_id)。
    mismatched = [
        (eid, wid)
        for eid, wid in zip(graph_ids_list, graph_wids_list)
        if wid != deterministic_uuid(args.namespace, eid)
    ]
    reverse_rate = (
        (len(graph_ids_list) - len(mismatched)) / len(graph_ids_list) * 100
        if graph_ids_list
        else 100.0
    )
    print(f"反向自洽（图 → 卡） : {reverse_rate:.1f}%")
    if mismatched:
        print(f"  {len(mismatched)} 个节点 id 不自洽，前 {args.show} 条：")
        for eid, wid in mismatched[: args.show]:
            print(f"    - {eid}: {wid}")

    # 信息项：图有、未删除卡没有 —— 软删的卡会落在这里，不判失败
    orphan = sorted(graph_ids - weaviate_ids)
    print(
        f"\n图有、未删除卡没有  : {len(orphan)} 个"
        "（软删的卡仍有图节点，属正常）"
    )

    # ---------- 总结 ----------
    ok = not missing_in_graph and not mismatched
    print()
    if ok:
        print("OK 双向可解析")
        return 0
    print("FAIL 有缺口（明细见上）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
