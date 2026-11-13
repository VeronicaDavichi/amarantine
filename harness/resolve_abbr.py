#!/usr/bin/env python3
"""把语料里的拼音缩写解析成全称，并写进图（spec 048）。

用法：

    python3 harness/resolve_abbr.py --session qq-876081394 --namespace qq-505456846 --apply
    python3 harness/resolve_abbr.py --session qq-876081394            # 只看结果

流程：

1. 从 `raw_quote` 取该会话全部原文
2. 扫描独立字母 token（2~6 位），频次 ≥ `--min-freq`
3. 每个 token：nbnhhsh 候选 + 本地词典 → 用共现打分
4. 有明确赢家才建 `(:Alias)-[:SAME_SENSE]->(:Alias)`；否则只报告
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

from memory.services import abbr as A  # noqa: E402

DB = ROOT / "back_end" / "data" / "memory.db"
NEO4J = "http://127.0.0.1:7474/db/neo4j/tx/commit"

SAME_SENSE_CYPHER = """
MERGE (a:Alias {namespace: $namespace, name: $abbr})
MERGE (b:Alias {namespace: $namespace, name: $full})
MERGE (a)-[r:SAME_SENSE]->(b)
SET r.source = $source, r.confidence = $confidence,
    r.updated_at = $now, r.ambiguous = false
RETURN type(r) AS t
"""


def cy(statement: str, params: dict, user: str = "neo4j", password: str = "password"):
    req = urllib.request.Request(
        NEO4J,
        data=json.dumps({"statements": [{"statement": statement, "parameters": params}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()},
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())["results"][0]["data"]


def load_texts(session: str, namespace: str) -> list[str]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(con.execute(
        "SELECT text FROM raw_quote WHERE namespace=? AND session_id=?", (namespace, session)))
    con.close()
    return [r[0] or "" for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="拼音缩写解析（spec 048）")
    ap.add_argument("--session", required=True)
    ap.add_argument("--namespace", default="qq-505456846")
    ap.add_argument("--min-freq", type=int, default=1)
    ap.add_argument("--apply", action="store_true", help="写 SAME_SENSE 边")
    args = ap.parse_args()

    texts = load_texts(args.session, args.namespace)
    if not texts:
        print(f"会话 {args.session} 没有原文")
        return 1
    print(f"原文 {len(texts)} 条")

    counter = A.scan_abbr_tokens(texts)
    cache = A.AbbrCache()
    local = A.load_local_dict()
    results = []
    for token, freq in counter.most_common(40):
        if freq < args.min_freq:
            continue
        got = A.resolve_abbr(token, texts, cache=cache, local=local)
        if not got["candidates"]:
            continue
        results.append((token, freq, got))

    print(f"\n候选 {len(results)} 个：")
    for token, freq, got in results:
        top = got["candidates"][0] if got["candidates"] else None
        mark = "✓" if got["picked"] else "?"
        print(f"  {mark} {token:<6} freq={freq:<3} 最优={top}  "
              f"picked={got['picked']} ambiguous={got['ambiguous']}")
        print(f"      候选: {got['candidates'][:4]}")

    picked = [(t, g["picked"], g["candidates"][0][1]) for t, _f, g in results if g["picked"]]
    print(f"\n可建边 {len(picked)} 条")
    if args.apply and picked:
        for token, full, score in picked:
            cy(SAME_SENSE_CYPHER, {
                "namespace": args.namespace, "abbr": token, "full": full,
                "source": "nbnhhsh", "confidence": min(1.0, score / 10.0),
                "now": "2026-11-13T00:00:00Z",
            })
            print(f"  + ({token})-[:SAME_SENSE]->({full})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
