#!/usr/bin/env python3
"""把「本人别名的 Person 节点」合并到 self Person（spec 050 延伸）。

## 为什么需要

同一个号有 19 个群名片。实体抽取会为其中几个各建一个 `Person`
（实测图里有独立的 `海嗣`），于是「我」被拆成很多节点：
关系边分散、whoami 看不到、查询「我说过什么」查不全。

本脚本把「名字属于本人别名」的 Person 节点合并到 `acct_<self_uid>`：
边搬过去 → 删原节点。幂等，可重跑。

用法：

    python3 harness/merge_self_persons.py --namespace qq-10000000001
    python3 harness/merge_self_persons.py --namespace qq-10000000001 --apply
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
    ap = argparse.ArgumentParser(description="合并本人别名的 Person 节点")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-prefix", action="store_true",
                    help="不合并「本人别名+动词残片」的节点（默认合并）")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services.self_identity import resolve_self

    settings = load_settings_from_project()
    got = resolve_self(settings, namespace=args.namespace)
    uid = got.get("uid") or ""
    if not uid:
        print("✗ 没能解析出本人：", got.get("reason"))
        return 2
    key = f"acct_{uid}"
    names = sorted({*(got.get("names") or []), *(got.get("nicknames") or []),
                    got.get("canonical") or ""} - {""})
    print(f"self key={key}，本人名字 {len(names)} 个")

    rows = cy(
        "MATCH (p:Person {namespace:$ns}) "
        "WHERE p.name IN $names AND coalesce(p.key,'') <> $key "
        "OPTIONAL MATCH (p)-[r]->(t) "
        "RETURN p.key AS k, p.name AS n, count(r) AS outs",
        {"ns": args.namespace, "names": names, "key": key})

    # 提取层会把名字和动词粘在一起（实测 `海嗣向潘`/`海嗣吐`/`海嗣用`）——
    # 这些仍然是「我」，按「本人别名 + ≤3 字残片」的前缀规则一并合并。
    # 只认 ≤3 字残片：否则 `苏苏的朋友` 这种真别人的名字会被误并。
    if not args.no_prefix:
        pref = cy(
            "MATCH (p:Person {namespace:$ns}) "
            "WHERE coalesce(p.key,'') <> $key "
            "RETURN p.key AS k, p.name AS n, 0 AS outs",
            {"ns": args.namespace, "key": key})
        for r in pref:
            name = r["row"][1] or ""
            for al in sorted(names, key=len, reverse=True):
                rest = name[len(al):] if name.startswith(al) and name != al else None
                if rest is not None and 0 < len(rest) <= 3:
                    rows.append({"row": [r["row"][0], name, 0]})
                    break
    todo = [(r["row"][0], r["row"][1], r["row"][2]) for r in rows]
    print(f"待合并的 Person 节点：{len(todo)} 个")
    for k, n, outs in todo:
        print(f"  {n!r:<28} key={k} 出边={outs}")

    if not todo:
        print("\n没有需要合并的节点（已经干净）")
        return 0
    if not args.apply:
        print("\n（dry-run；加 --apply 才合并）")
        return 0

    # 确保 self Person 存在
    cy("MERGE (p:Person {namespace:$ns, key:$key}) "
       "SET p.name = coalesce(p.name, $name), p.is_self = true RETURN 1",
       {"ns": args.namespace, "key": key,
        "name": got.get("canonical") or (names[0] if names else "")})

    moved = 0
    for old_key, old_name, _outs in todo:
        if not old_key:
            # 没有 key 的节点：直接按名字匹配
            cy("MATCH (p:Person {namespace:$ns, name:$name}) "
               "WHERE p.key IS NULL DETACH DELETE p", {"ns": args.namespace, "name": old_name})
            continue
        # 出边
        for pred in PREDS:
            cy(f"""
            MATCH (old:Person {{namespace:$ns, key:$old}})-[r:{pred}]->(t)
            MATCH (self:Person {{namespace:$ns, key:$self_key}})
            MERGE (self)-[r2:{pred}]->(t)
            ON CREATE SET r2 = properties(r)
            DELETE r
            RETURN 1
            """, {"ns": args.namespace, "old": old_key, "self_key": key})
            # 入边
            cy(f"""
            MATCH (h)-[r:{pred}]->(old:Person {{namespace:$ns, key:$old}})
            MATCH (self:Person {{namespace:$ns, key:$self_key}})
            MERGE (h)-[r2:{pred}]->(self)
            ON CREATE SET r2 = properties(r)
            DELETE r
            RETURN 1
            """, {"ns": args.namespace, "old": old_key, "self_key": key})
        moved += 1
        cy("MATCH (old:Person {namespace:$ns, key:$old}) DETACH DELETE old",
           {"ns": args.namespace, "old": old_key})
        print(f"  已合并 {old_name!r} → {key}")

    after = cy("MATCH (self:Person {namespace:$ns, key:$key})-[r]->(t) "
               "WHERE type(r) IN $p RETURN count(r) AS c",
               {"ns": args.namespace, "key": key, "p": list(PREDS)})[0]["row"][0]
    print(f"\n合并 {moved} 个节点；self Person 现在出边 {after} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
