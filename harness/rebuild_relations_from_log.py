# -*- coding: utf-8 -*-
"""清空关系边，按本次（已排除营销）的重建结果重写。

为什么这么做：`build_relations` 只合并不改删，所以历史上由营销卡抽出的边
（`运营 -OWNS-> 尾款` 之类）会一直留着。用户 2026-11-19 定了「营销不做关系
判定」，这些边就该消失 —— 所以先清空，再用**这次排除营销后**的结果重建。

重建数据直接从上一次运行的日志里取（避免再跑 25 分钟且结果会变）。
"""
import base64
import json
import re
import sys
import urllib.request

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/harness")
sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")
sys.stdout.reconfigure(encoding="utf-8")

NS = "qq-505456846"
NEO = "http://127.0.0.1:7474/db/neo4j/tx/commit"
PREDS = ["OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON"]
LOG = "/tmp/rel_nomk.log"
APPLY = "--apply" in sys.argv


def cy(q, params=None):
    stmt = {"statement": q}
    if params:
        stmt["parameters"] = params
    r = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    return json.loads(urllib.request.urlopen(r, timeout=180).read())["results"][0]["data"]


pat = re.compile(r"陈述\[llm\]: (.+?) -\[(\w+)\]-> (.+)$")
claims = []
for line in open(LOG, encoding="utf-8", errors="ignore"):
    m = pat.search(line.strip())
    if m:
        claims.append((m.group(1).strip(), m.group(2), m.group(3).strip()))
uniq = list(dict.fromkeys(claims))
print(f"日志里的 claim {len(claims)} 条，去重 {len(uniq)} 条")

if not APPLY:
    print("（dry-run；加 --apply 才清空并重写）")
    sys.exit(0)

n = cy("MATCH ()-[r]->() WHERE type(r) IN $p DELETE r RETURN count(*) AS c",
       {"p": PREDS})[0]["row"][0]
print(f"已删旧边 {n} 条")

# 复用 build_relations 的端点解析与写边
import importlib.util

spec = importlib.util.spec_from_file_location(
    "br", "/mnt/e/PycharmProjects/amarantine/harness/build_relations.py")
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

from neo4j import GraphDatabase

from memory.config import load_settings

settings = load_settings()
drv = GraphDatabase.driver(settings.NEO4J_URI,
                           auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
try:
    with drv.session() as session:
        names = {h for h, _p, _t in uniq} | {t for _h, _p, t in uniq}
        resolved, missing, anchor = br.resolve_ends(session, NS, names)
        if anchor:
            session.run(br.ENSURE_PERSON_CYPHER, namespace=NS, rows=anchor,
                        now="2026-11-19T00:00:00+00:00")
        if missing:
            session.run(br.ENSURE_ENTITY_CYPHER, namespace=NS,
                        names=sorted(missing), now="2026-11-19T00:00:00+00:00")
            for name in missing:
                resolved[name] = ("Entity", f"ent_{name}")
        by_pred: dict[str, list[dict]] = {}
        for h, pred, t in uniq:
            hk, tk = resolved.get(h), resolved.get(t)
            if not hk or not tk:
                continue
            by_pred.setdefault(pred, []).append({
                "head": h, "tail": t, "label": "probable", "confidence": 0.6,
                "evidence": [], "source": "llm",
                "head_kind": hk[0], "head_key": hk[1],
                "tail_kind": tk[0], "tail_key": tk[1],
            })
        total = 0
        for pred, items in by_pred.items():
            res = session.run(br.write_cypher(pred), namespace=NS, items=items,
                              now="2026-11-19T00:00:00+00:00").single()
            c = int(res["n"]) if res else 0
            total += c
            print(f"  写入 {pred}: {c} / {len(items)}")
        print(f"共写入 {total} 条边")
finally:
    drv.close()
