#!/usr/bin/env python3
"""自动修人名碎片：判定 → 合并 or 抑制（不需要人工逐条裁决）。

## 判定规则（全部可复现）

`frag` 是 `alias` 的一部分，且该 alias **只属于一个账号**，再看「多出的部分」：

| 多出的是什么 | 处理 | 理由 |
|---|---|---|
| 纯装饰（emoji/标点/数字/括号外无实义） | **合并**到该账号的 Person | `💤`、`？`、`-404-` 不是名字的一部分 |
| 有实义的词（`+ OLIVIA`、`New`、`扑街少年`） | **抑制**该 frag（不建 Entity） | 碎片是分词产物，不代表独立实体 |

两者都**不需要人逐条看**：合并只发生在「去掉装饰就是完整名字」这种确定情形；
其余一律不建实体，靠下一轮重灌自动生效。

用法：

    python3 harness/auto_entity_repair.py --namespace qq-505456846            # 报告
    python3 harness/auto_entity_repair.py --namespace qq-505456846 --apply    # 落库+改图
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.name_marker import has_name_boundary  # noqa: E402

ID_DIR = ROOT / "back_end" / "data" / "corpus" / "qq-identities"
NEO = "http://127.0.0.1:7474/db/neo4j/tx/commit"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3"
                   "\u202a-\u202e\u200b-\u200f]")


def cy(q: str, params: dict | None = None) -> list[list]:
    import base64
    import urllib.request

    stmt: dict = {"statement": q}
    if params:
        stmt["parameters"] = params
    req = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())["results"][0]["data"]


def deco_only(rest: str) -> bool:
    """剩下的是不是纯装饰（emoji/标点/数字/空白）。"""
    return bool(re.fullmatch(r"[\d\s\W_]*", EMOJI.sub("", rest)))


def name2uids(namespace: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for r in cy(f"MATCH (a:Account {{namespace:$ns}})-[:DISPLAYED_AS]->(al:Alias {{namespace:$ns}}) "
                f"RETURN al.name AS n, a.uid AS uid", {"ns": namespace}):
        if r["row"][0]:
            out[r["row"][0]].add(r["row"][1])
    return out


def fragments(namespace: str) -> list[dict]:
    owner = name2uids(namespace)
    person_names = {r["row"][0] for r in cy(
        f"MATCH (p:Person {{namespace:$ns}}) RETURN p.name AS n", {"ns": namespace})}
    rows = cy(f"""
    MATCH (e:Entity {{namespace:$ns}})
    WHERE coalesce(e.kind,'topic') = 'topic'
    OPTIONAL MATCH (ev:Event {{namespace:$ns}})-[:MENTIONS]->(e)
    RETURN e.name AS name, count(DISTINCT ev) AS cards
    """, {"ns": namespace})

    out: list[dict] = []
    for r in rows:
        name, cards = r["row"][0], (r["row"][1] or 0)
        if not name or name in person_names:
            continue
        owners: set[str] = set()
        alias_hit = ""
        for alias, uids in owner.items():
            if alias != name and name in alias:
                owners |= uids
                if not alias_hit or len(alias) < len(alias_hit):
                    alias_hit = alias
        if len(owners) != 1 or not alias_hit:
            continue
        if not has_name_boundary(alias_hit, name):
            continue  # 连续串里切出来的（谐音/拼词），整串才算名字
        rest = alias_hit.replace(name, "", 1)
        out.append({"frag": name, "cards": cards, "alias": alias_hit,
                    "rest": rest, "uid": next(iter(owners)),
                    "merge": deco_only(rest)})
    out.sort(key=lambda x: -x["cards"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="自动修人名碎片（合并 or 抑制）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    items = fragments(args.namespace)
    merge = [x for x in items if x["merge"]]
    suppress = [x for x in items if not x["merge"]]
    print(f"人名碎片 {len(items)} 个：自动合并 {len(merge)}，自动抑制 {len(suppress)}\n")

    print("=== 自动合并（去掉装饰就是完整名字）===")
    for x in merge:
        print(f"  {x['frag']:<14} ⊂ {x['alias']!r:<26} 装饰={x['rest']!r:<10} {x['cards']} 卡")
    print("\n=== 自动抑制（多出的部分是实义词）===")
    for x in suppress[:20]:
        print(f"  {x['frag']:<14} ⊂ {x['alias']!r:<26} 多出={x['rest']!r:<12} {x['cards']} 卡")

    if not args.apply:
        print("\n（dry-run；加 --apply 才写库/改图）")
        return 0

    # 1) 抑制：写表
    from memory.config import load_settings
    from memory.services.entity_suppress import store_for

    settings = load_settings()
    n = store_for(settings).add(
        args.namespace, [x["frag"] for x in suppress],
        reason="name_fragment",
        detail="别名的一部分，不是独立实体（自动判定）",
    )
    print(f"\n写入抑制表 {n} 条")

    # 2) 合并：MENTIONS 与关系边都转到 Person，再删碎片节点
    #
    # 关系类型无法参数化（Cypher 不允许 `MERGE (a)-[r:$type]->(b)`），
    # 所以按白名单逐类跑，且两个方向都要搬（碎片可能当主语也可能当宾语）。
    PREDS = ("OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON")
    merged = 0
    for x in merge:
        pkey = f"acct_{x['uid']}"
        params = {"ns": args.namespace, "frag": x["frag"],
                  "pkey": pkey, "alias": x["alias"]}
        # 2.1 Person 节点（账号锚定，spec 045）
        cy("""
        MERGE (p:Person {namespace:$ns, key:$pkey})
        ON CREATE SET p.name = $alias, p.created_at = timestamp()
        SET p:Person
        RETURN 1
        """, params)
        # 2.2 MENTIONS（事件 → 实体）
        cy("""
        MATCH (ev:Event {namespace:$ns})-[m:MENTIONS]->(e:Entity {namespace:$ns, name:$frag})
        MATCH (p:Person {namespace:$ns, key:$pkey})
        MERGE (ev)-[:MENTIONS {created_at: m.created_at}]->(p)
        DELETE m
        RETURN 1
        """, params)
        # 2.3 关系边：碎片当宾语
        for pred in PREDS:
            cy(f"""
            MATCH (h)-[r:{pred}]->(e:Entity {{namespace:$ns, name:$frag}})
            MATCH (p:Person {{namespace:$ns, key:$pkey}})
            MERGE (h)-[r2:{pred}]->(p)
            ON CREATE SET r2 = properties(r)
            DELETE r
            RETURN 1
            """, params)
        # 2.4 关系边：碎片当主语
            cy(f"""
            MATCH (e:Entity {{namespace:$ns, name:$frag}})-[r:{pred}]->(t)
            MATCH (p:Person {{namespace:$ns, key:$pkey}})
            MERGE (p)-[r2:{pred}]->(t)
            ON CREATE SET r2 = properties(r)
            DELETE r
            RETURN 1
            """, params)
        # 2.5 删碎片节点
        cy("MATCH (e:Entity {namespace:$ns, name:$frag}) DETACH DELETE e", params)
        merged += 1
    print(f"合并到 Person {merged} 个")

    # 3) 抑制的名字：删掉现有 Entity 节点（之后写入会被过滤，不会再长回来）
    for x in suppress:
        cy("MATCH (e:Entity {namespace:$ns, name:$frag}) DETACH DELETE e",
           {"ns": args.namespace, "frag": x["frag"]})
    print(f"删除被抑制的 Entity {len(suppress)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
