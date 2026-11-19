#!/usr/bin/env python3
"""三处状态一起备份（spec 049 G2）。

## 为什么必须「一起」

记忆分三处：SQLite（原文/job/抑制表）、Weaviate（卡片 + 向量）、
Neo4j（图）。只备一处会出现「有卡没图」「有原文没卡」这类不一致，
恢复后行为不可复现。所以本脚本一次备齐，并写一份 **manifest** 记下各自条数 ——
恢复时用它校验，而不是靠人眼看。

## 为什么不用 weaviate/neo4j 的原生备份

两个都需要事先开特性（Weaviate 要 `backup-filesystem` 模块，Neo4j 要
`neo4j-admin dump` 且通常要停库）。对自托管第一版，**导出成 JSONL** 更省事，
也便于人看/迁移，代价是恢复慢一点（几千条量级完全够）。

用法：

    python3 harness/backup_state.py --namespace qq-10000000001 --out backups/2026-11-19
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
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
    return json.loads(urllib.request.urlopen(req, timeout=180).read())["results"][0]["data"]


def gql(query: str) -> dict:
    req = urllib.request.Request(
        f"{WEAVIATE}/v1/graphql", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


def backup_sqlite(out: Path) -> dict:
    """用 sqlite3 的 backup API 复制（在线安全，不会拷到半写状态）。"""
    dst = out / "memory.db"
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con = sqlite3.connect(dst)
    try:
        src.backup(con)
    finally:
        con.close()
        src.close()
    # 备份出来的 db 不需要 WAL 副本；留着会被误当成「数据文件」一起提交
    for suffix in ("-wal", "-shm"):
        side = Path(str(dst) + suffix)
        if side.exists():
            side.unlink()
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    counts = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            pass
    con.close()
    return counts


def backup_weaviate(out: Path, namespace: str) -> int:
    """把某个 namespace 的卡片逐页导出成 JSONL。"""
    path = out / "cards.jsonl"
    n = 0
    offset = 0
    with path.open("w", encoding="utf-8") as fh:
        while True:
            q = ('{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
                 f'valueText:"{namespace}"}}, limit: 200, offset: {offset}) {{ '
                 'event_id namespace summary date_start date_end types importance pinned '
                 'pinned_source unresolved entity_names raw_quote_ids retention '
                 'recall_count confirmed_by_use review_verdict vocab_version reviewer '
                 'observed_at session_id resolves } } }')
            try:
                data = gql(q)
            except Exception as exc:  # noqa: BLE001 - 备份失败要明确报错
                raise RuntimeError(f"Weaviate 导出失败: {exc}") from exc
            rows = (((data.get("data") or {}).get("Get") or {}).get("MemoryEvent") or [])
            if not rows:
                break
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
            if len(rows) < 200:
                break
            offset += 200
    return n


def backup_neo4j(out: Path, namespace: str) -> dict:
    """导出节点与边（JSONL）。"""
    nodes = cy("MATCH (n {namespace:$ns}) RETURN labels(n) AS labels, properties(n) AS props",
               {"ns": namespace})
    edges = cy("MATCH (a {namespace:$ns})-[r]->(b {namespace:$ns}) "
               "RETURN labels(a) AS al, a.key AS ak, a.uid AS au, a.name AS an, "
               "type(r) AS t, properties(r) AS rp, "
               "labels(b) AS bl, b.key AS bk, b.uid AS bu, b.name AS bn",
               {"ns": namespace})
    with (out / "graph_nodes.jsonl").open("w", encoding="utf-8") as fh:
        for r in nodes:
            fh.write(json.dumps({"labels": r["row"][0], "props": r["row"][1]},
                                ensure_ascii=False) + "\n")
    with (out / "graph_edges.jsonl").open("w", encoding="utf-8") as fh:
        for r in edges:
            fh.write(json.dumps({"from": {"labels": r["row"][0], "key": r["row"][1],
                                          "uid": r["row"][2], "name": r["row"][3]},
                                 "type": r["row"][4], "props": r["row"][5],
                                 "to": {"labels": r["row"][6], "key": r["row"][7],
                                        "uid": r["row"][8], "name": r["row"][9]}},
                                ensure_ascii=False) + "\n")
    return {"nodes": len(nodes), "edges": len(edges)}


def main() -> int:
    ap = argparse.ArgumentParser(description="三处状态一起备份")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else ROOT / "backups" / stamp
    out.mkdir(parents=True, exist_ok=True)

    print(f"备份到 {out}")
    sqlite_counts = backup_sqlite(out)
    print(f"  SQLite: {sqlite_counts}")

    cards = backup_weaviate(out, args.namespace)
    print(f"  Weaviate 卡片: {cards}")

    graph = backup_neo4j(out, args.namespace)
    print(f"  Neo4j: 节点 {graph['nodes']} / 边 {graph['edges']}")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "sqlite": sqlite_counts,
        "weaviate_cards": cards,
        "graph_nodes": graph["nodes"],
        "graph_edges": graph["edges"],
        "files": sorted(p.name for p in out.iterdir() if p.is_file()),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nmanifest → {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
