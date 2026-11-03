#!/usr/bin/env python3
"""A8：从事件卡重建图（`Event` 节点 + `weaviate_id` 双向可解析）。

**为什么需要**：图只经写入路径产生，一旦 Neo4j 数据损坏或容器被重建，
没有任何恢复手段 —— 派生层与事实层的可重建性从来没被证明过。

用法（仓库根目录）：

    # 先看会重建多少（默认，不动图）
    ./harness/dev-linux.sh python ../harness/rebuild_graph.py --namespace work

    # 真写
    ./harness/dev-linux.sh python ../harness/rebuild_graph.py --namespace work --apply

**默认 dry-run** 是刻意的：重建是破坏性运维动作，先看一眼是基本礼貌。

只重建 `:Event` 节点本身与 `weaviate_id`；实体边、动作边等由
`write_event_node` 在写入路径产生，本脚本不重放（那需要原文和 LLM，
不是「从事实层重建」的范畴）。

退出码：0 = 成功（含 dry-run），1 = 有失败项。
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

MERGE_CYPHER = """
UNWIND $rows AS r
MERGE (e:Event {namespace: $namespace, event_id: r.event_id})
SET e.weaviate_id = r.weaviate_id,
    e.summary = r.summary,
    e.date_start = r.date_start
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="从事件卡重建图（A8）")
    ap.add_argument("--namespace", required=True, help="记忆域")
    ap.add_argument(
        "--apply", action="store_true", help="真的写图；不加则只报告"
    )
    args = ap.parse_args()

    settings = load_settings()

    store = WeaviateStore(settings)
    try:
        cards = store.list_all(args.namespace)
    except Exception as exc:  # noqa: BLE001 - 运维脚本把原因打清楚比 traceback 有用
        print(f"FATAL 读取事件卡失败: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if not cards:
        print("没有事件卡 —— 空库重建是幂等的（不报错，0 条）")
        return 0

    rows = [
        {
            "event_id": c["event_id"],
            "weaviate_id": deterministic_uuid(args.namespace, c["event_id"]),
            "summary": c.get("summary"),
            "date_start": c.get("date_start"),
        }
        for c in cards
    ]

    print(f"命名空间      : {args.namespace}")
    print(f"事件卡        : {len(rows)} 条")
    print(f"模式          : {'APPLY（真写）' if args.apply else 'DRY-RUN（不动图）'}")

    if not args.apply:
        print("\n将要 MERGE 的节点数 :", len(rows))
        print("示例 :", rows[0]["event_id"], "->", rows[0]["weaviate_id"])
        print("\n加 --apply 才会真的写。")
        return 0

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
            session.run(MERGE_CYPHER, rows=rows, namespace=args.namespace)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL 写图失败: {exc}", file=sys.stderr)
        return 1
    finally:
        driver.close()

    print(f"\n已 MERGE {len(rows)} 个 :Event 节点")
    print("用 check_rebuild.py 验证双向可解析。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
