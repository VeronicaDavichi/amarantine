#!/usr/bin/env python3
"""把已存在的关系边里「主语是本人的某个名字」重指到 self Person。

为什么需要一次性重指：关系边是按名字解析写进去的，`SELF_QQ` 是后加的。
重跑整层（25 分钟）能修，但这次只是把主语换个节点 —— 直接改边更快、也可复核。

用法：

    python3 harness/rebind_self_edges.py --namespace qq-10000000001
    python3 harness/rebind_self_edges.py --namespace qq-10000000001 --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

NEO = "http://127.0.0.1:7474/db/neo4j/tx/commit"
PREDS = ("OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON")


def cy(statement: str, params: dict | None = None) -> list[list]:
    stmt: dict = {"statement": statement}
    if params:
        stmt["parameters"] = params
    req = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    data = json.loads(urllib.request.urlopen(req, timeout=180).read())
    if data.get("errors"):
        raise SystemExit(f"Cypher 失败: {data['errors'][:1]}")
    return data["results"][0]["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description="把本人名字主语的关系边重指到 self Person")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services.self_identity import resolve_self

    settings = load_settings_from_project()
    got = resolve_self(settings, namespace=args.namespace)
    uid = got.get("uid") or ""
    if not uid:
        print("✗ 没能解析出本人（检查 .env 的 SELF_QQ 与成员表）")
        print("  说明：", got.get("reason"))
        return 2
    key = f"acct_{uid}"
    names = sorted({*(got.get("names") or []), *(got.get("nicknames") or []),
                    got.get("canonical") or ""} - {""})
    print(f"本人 uid={uid} key={key} 名字 {len(names)} 个")

    # 先看现在有多少边的主语是这些名字
    rows = cy(
        "MATCH (h)-[r]->(t) WHERE type(r) IN $p AND coalesce(h.name, h.uid) IN $names "
        "RETURN coalesce(h.name,h.uid) AS h, type(r) AS pred, coalesce(t.name,t.uid) AS t "
        "ORDER BY h",
        {"p": list(PREDS), "names": names})
    print(f"主语命中本人名字的边：{len(rows)} 条")
    for r in rows[:10]:
        print(f"  {r['row'][0]} -[{r['row'][1]}]-> {r['row'][2]}")

    if not args.apply:
        print("\n（dry-run；加 --apply 才改）")
        return 0

    # 确保 self Person 存在
    cy("MERGE (p:Person {namespace:$ns, key:$key}) "
       "SET p.name = coalesce(p.name, $name), p.is_self = true RETURN 1",
       {"ns": args.namespace, "key": key,
        "name": got.get("canonical") or (got.get("names") or [""])[0]})

    moved = 0
    for r in rows:
        head, pred, tail = r["row"][0], r["row"][1], r["row"][2]
        cy(f"""
        MATCH (h {{namespace:$ns}})-[r:{pred}]->(t {{namespace:$ns}})
        WHERE coalesce(h.name,h.uid) = $head AND coalesce(t.name,t.uid) = $tail
        MATCH (p:Person {{namespace:$ns, key:$key}})
        MERGE (p)-[r2:{pred}]->(t)
        ON CREATE SET r2 = properties(r)
        DELETE r
        RETURN 1
        """, {"ns": args.namespace, "head": head, "tail": tail, "key": key})
        moved += 1
    print(f"\n已重指 {moved} 条边 → {key}")

    after = cy("MATCH (p:Person {namespace:$ns, key:$key})-[r]->(t) "
               "WHERE type(r) IN $p RETURN count(r) AS c",
               {"ns": args.namespace, "key": key, "p": list(PREDS)})[0]["row"][0]
    print(f"self Person 现在的出边：{after} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
