#!/usr/bin/env python3
"""从备份还原三处状态，并校验条数（spec 049 G2 / A3）。

## 设计取舍

- **卡片带向量**：`backup_state.py` 导出了 `_additional { vector }`，还原时原样写回。
  不重新 embed —— 换过 embedding 模型就会造成新旧向量混用（A26 那条红线）。
- **按 namespace 还原**：备份里只含一个 namespace 的数据，还原默认写回原 namespace，
  `--into` 可以导入到另一个 namespace（演练用，避免动真实数据）。
- **幂等**：SQLite 用 `INSERT OR REPLACE`，Weaviate 用固定 UUID（`deterministic_uuid`），
  Neo4j 用 `MERGE`。重跑不会翻倍。
- **校验**：还原后重新计数，与 `manifest.json` 比；不一致就报出来并返回非零。

用法：

    # 演练：把备份导进 verify049，不动真实数据
    python3 harness/restore_state.py --from backups/20261119-105128 --into verify049

    # 真还原
    python3 harness/restore_state.py --from backups/20261119-105128
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

DB = ROOT / "back_end" / "data" / "memory.db"
NEO = "http://127.0.0.1:7474/db/neo4j/tx/commit"
WEAVIATE = "http://127.0.0.1:8081"


def cy(q: str, params: dict | None = None) -> list[list]:
    stmt: dict = {"statement": q}
    if params:
        stmt["parameters"] = params
    req = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())["results"][0]["data"]


def gql(query: str) -> dict:
    req = urllib.request.Request(
        f"{WEAVIATE}/v1/graphql", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def count_cards(namespace: str) -> int:
    """数某 namespace 的卡片数（GraphQL Aggregate）。"""
    q = (
        "{ Aggregate { MemoryEvent(where: {path:[\"namespace\"], operator: Equal, "
        "valueText: \"" + namespace + "\"}) { meta { count } } } }"
    )
    data = gql(q)
    rows = (((data.get("data") or {}).get("Aggregate") or {}).get("MemoryEvent") or [])
    return int(rows[0].get("meta", {}).get("count", 0)) if rows else 0


def restore_sqlite(src: Path, namespace: str, into: str) -> dict[str, int]:
    """按 namespace 还原各表（缺 `namespace` 列的表跳过）。"""
    con = sqlite3.connect(src, timeout=30)
    con.row_factory = sqlite3.Row
    tables = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out: dict[str, int] = {}
    with sqlite3.connect(DB, timeout=30) as dst:
        for t in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            dst_cols = [r[1] for r in dst.execute(f"PRAGMA table_info({t})")]
            if "namespace" not in cols or "namespace" not in dst_cols:
                continue
            rows = con.execute(f"SELECT * FROM {t} WHERE namespace=?", (namespace,)).fetchall()
            if not rows:
                continue
            n = 0
            for row in rows:
                data = dict(row)
                if into != namespace:
                    data["namespace"] = into
                keys = list(data)
                sql = (f"INSERT OR REPLACE INTO {t} ({','.join(keys)}) "
                       f"VALUES ({','.join('?' * len(keys))})")
                dst.execute(sql, [data[k] for k in keys])
                n += 1
            out[t] = n
        dst.commit()
    con.close()
    return out


def restore_cards(src: Path, into: str) -> int:
    """把 cards.jsonl 写回 Weaviate（带向量，固定 UUID）。"""
    import httpx

    from memory.config import load_settings
    from memory.services.weaviate_store import deterministic_uuid

    settings = load_settings()
    n = 0
    with httpx.Client(timeout=60) as c, (src / "cards.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            extra = row.pop("_additional", {}) or {}
            vec = extra.get("vector")
            if not vec:
                raise SystemExit("备份里没有向量 —— 请用新版 backup_state.py 重备")
            row["namespace"] = into
            # 用确定性 UUID（与原写入一致），重跑幂等；备份里的 id 只在同 namespace 有效
            body = {"class": "MemoryEvent",
                    "id": deterministic_uuid(into, row["event_id"]),
                    "properties": row, "vector": vec}
            r = c.post(f"{WEAVIATE}/v1/objects", json=body)
            if r.status_code not in (200, 201):
                raise SystemExit(f"写卡片失败 {row['event_id']}: {r.status_code} {r.text[:200]}")
            n += 1
    return n


def restore_graph(src: Path, namespace: str, into: str) -> dict[str, int]:
    """还原节点与边（MERGE 幂等）。"""
    nodes = [json.loads(l) for l in (src / "graph_nodes.jsonl").open(encoding="utf-8")]
    edges = [json.loads(l) for l in (src / "graph_edges.jsonl").open(encoding="utf-8")]
    for node in nodes:
        labels = ":".join(node["labels"]) or "Node"
        props = dict(node["props"])
        props["namespace"] = into
        cy(f"MERGE (n:`{labels}` {{namespace:$ns, key:$key}}) SET n += $props",
           {"ns": into, "key": props.get("key") or props.get("event_id")
            or props.get("uid") or props.get("name") or props.get("time_key") or "",
            "props": props})
    done = 0
    for e in edges:
        pred = e["type"]
        cy(f"""
        MATCH (a {{namespace:$ns}}) WHERE coalesce(a.key,a.event_id,a.uid,a.name,a.time_key) = $ak
        MATCH (b {{namespace:$ns}}) WHERE coalesce(b.key,b.event_id,b.uid,b.name,b.time_key) = $bk
        MERGE (a)-[r:`{pred}`]->(b) SET r += $props
        RETURN 1
        """, {"ns": into, "ak": e["from"].get("key") or e["from"].get("uid")
              or e["from"].get("name"), "bk": e["to"].get("key") or e["to"].get("uid")
              or e["to"].get("name"), "props": e.get("props") or {}})
        done += 1
    return {"nodes": len(nodes), "edges": done}


def main() -> int:
    ap = argparse.ArgumentParser(description="从备份还原三处状态并校验条数")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--into", default="", help="导入到另一个 namespace（演练用）")
    ap.add_argument("--skip-graph", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    ns = manifest["namespace"]
    into = args.into or ns
    print(f"还原 {src} → namespace {into}")

    sql = restore_sqlite(src / "memory.db", ns, into)
    print(f"  SQLite: {sql}")

    cards = restore_cards(src, into)
    print(f"  卡片写入: {cards}")

    if not args.skip_graph:
        g = restore_graph(src, ns, into)
        print(f"  图: 节点 {g['nodes']} / 边 {g['edges']}")

    # 校验
    got_cards = count_cards(into)
    print(f"\n校验卡片数：备份 {manifest['weaviate_cards']} / 还原后 {got_cards}")
    ok = got_cards == manifest["weaviate_cards"]
    print("A3 卡片条数一致 ✓" if ok else "A3 卡片条数不一致 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
