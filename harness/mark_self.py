#!/usr/bin/env python3
"""把「本人」标记写进图（spec 050 延伸）。

在 `SELF_QQ` 解析出的 uid 上设置 `is_self=true`，并确保对应的 `Person`
以账号锚定的键存在（spec 045：`acct_<uid>`）。之后查询就能区分
「我说的话」和「别人的话」。

用法：

    python3 harness/mark_self.py --namespace qq-10000000001            # 只看解析结果
    python3 harness/mark_self.py --namespace qq-10000000001 --apply    # 写图
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


def cy(statement: str, params: dict | None = None) -> list[list]:
    stmt: dict = {"statement": statement}
    if params:
        stmt["parameters"] = params
    req = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["results"][0]["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description="把「本人」标记写进图")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services.self_identity import resolve_self

    try:
        settings = load_settings_from_project()
    except Exception:  # noqa: BLE001
        from memory.config import load_settings

        settings = load_settings()

    got = resolve_self(settings, namespace=args.namespace)
    print(f"configured={got['configured']} qq={got['qq'] or '（未配）'}")
    if got.get("reason"):
        print(f"说明：{got['reason']}")
    if not got["uid"]:
        return 1

    print(f"uid: {got['uid']}")
    print(f"账号昵称：{got.get('canonical') or '（无）'}")
    print(f"群名片（{len(got['names'])} 个）：{got['names'][:8]}")
    print(f"出现在 {len(got['namespaces_seen'])} 个群的成员表里")

    if not args.apply:
        print("\n（dry-run；加 --apply 才写图）")
        return 0

    # 1) Account 打标
    cy("MATCH (a:Account {namespace:$ns, uid:$uid}) "
       "SET a.is_self = true, a.self_qq = $qq RETURN 1",
       {"ns": args.namespace, "uid": got["uid"], "qq": got["qq"]})
    # 2) Person（账号锚定键）打标 + 名字回填
    from memory.services.self_identity import preferred_name

    name = got.get("canonical") or preferred_name(got["names"], fallback=got["qq"])
    cy("MERGE (p:Person {namespace:$ns, key:$key}) "
       "ON CREATE SET p.name = $name, p.created_at = timestamp() "
       "SET p:Person, p.is_self = true, p.self_qq = $qq RETURN 1",
       {"ns": args.namespace, "key": f"acct_{got['uid']}", "name": name,
        "qq": got["qq"]})

    # 本人 Person 的名字用 canonical（账号昵称）：它比群名片权威。
    cy("MATCH (p:Person {namespace:$ns, key:$key}) SET p.name = $name RETURN 1",
       {"ns": args.namespace, "key": f"acct_{got['uid']}", "name": name})

    marked = cy("MATCH (n {namespace:$ns}) WHERE n.is_self = true "
                "RETURN labels(n)[0] AS l, coalesce(n.name, n.uid) AS n",
                {"ns": args.namespace})
    print("\n已标记：")
    for r in marked:
        print(f"  {r['row'][0]:<10} {r['row'][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
