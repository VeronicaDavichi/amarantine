#!/usr/bin/env python3
"""生成人工标注数据（spec 039 / 041），供 docs/labeling.html 使用。

三组：

- `statements`：含「负责/维护/对接/协作/依赖/一起」的句子，
  一半是规则抽出来的（测 precision），一半是规则漏掉的（测 recall）
- `behaviors`：人-动作-对象组（含 1~3 条证据），标「稳定关系 / 偶发 / 错误」
- `senses`：指称候选（term + 上下文 + 机器给的解释）

用法：

    python3 harness/make_labeling_data.py --out docs/labeling-data.js
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import random
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.reference import build_candidates, extract_signals  # noqa: E402
from memory.services.relations import extract_statements  # noqa: E402

NEO4J = "http://127.0.0.1:7474/db/neo4j/tx/commit"
NS_QQ = "qq-505456846"
NS_DING = "njzq-dingtalk"
STMT_HINT = re.compile(r"(负责|维护|对接|协作|依赖|一起|分管|管着)")


def cy(statement: str) -> list[list]:
    req = urllib.request.Request(
        NEO4J,
        data=json.dumps({"statements": [{"statement": statement}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()},
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["results"][0]["data"]


def sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？\n]", text or "")
    return [p.strip() for p in parts if 20 <= len(p.strip()) <= 160]


def build_statements(limit: int = 20) -> list[dict]:
    rows = cy(f"MATCH (e:Event {{namespace:'{NS_QQ}'}}) RETURN e.summary LIMIT 3000")
    rows += cy(f"MATCH (e:Event {{namespace:'{NS_DING}'}}) RETURN e.summary LIMIT 3000")
    pool: list[str] = []
    for r in rows:
        for s in sentences((r.get("row") or [""])[0] or ""):
            if STMT_HINT.search(s):
                pool.append(s)
    random.seed(42)
    random.shuffle(pool)

    items: list[dict] = []
    picked: set[str] = set()
    # 1) 规则抽得到的（测 precision）
    for s in pool:
        claims = extract_statements(s)
        if not claims:
            continue
        c = claims[0]
        key = f"{c.head}|{c.predicate}|{c.tail}"
        if key in picked:
            continue
        picked.add(key)
        items.append({
            "kind": "statement",
            "id": f"stmt-pos-{len(items)+1}",
            "evidence": s,
            "machine": {"head": c.head, "predicate": c.predicate, "tail": c.tail,
                        "extracted": True},
            "source": "rule_hit",
        })
        if len([i for i in items if i["source"] == "rule_hit"]) >= limit // 2:
            break
    # 2) 规则漏掉的（测 recall）
    for s in pool:
        if extract_statements(s):
            continue
        items.append({
            "kind": "statement",
            "id": f"stmt-neg-{len(items)+1}",
            "evidence": s,
            "machine": {"head": None, "predicate": None, "tail": None, "extracted": False},
            "source": "rule_miss",
        })
        if len([i for i in items if i["source"] == "rule_miss"]) >= limit // 2:
            break
    return items


def build_behaviors(limit: int = 10) -> list[dict]:
    data = cy(
        f"MATCH (p:Person {{namespace:'{NS_QQ}'}})<-[:INVOLVES]-(e:Event)-[:ACTION]->(a:Action) "
        f"MATCH (e)-[:MENTIONS]->(m) "
        f"RETURN p.name, a.verb, m.name, e.event_id, e.summary LIMIT 5000")
    groups: dict[tuple, list[dict]] = {}
    for row in data:
        name, verb, target, eid, summary = (row.get("row") or [None] * 5)
        key = (name or "", verb or "", target or "")
        groups.setdefault(key, []).append({"event_id": eid, "evidence": summary})
    items = []
    for (person, verb, target), evs in sorted(groups.items(), key=lambda x: -len(x[1]))[:limit]:
        items.append({
            "kind": "behavior",
            "id": f"beh-{len(items)+1}",
            "person": person, "verb": verb, "target": target,
            "evidence_count": len(evs),
            "evidence": [e["evidence"] for e in evs[:3]],
            "source": "behavior",
        })
    return items


def build_senses(limit: int = 20) -> list[dict]:
    rows = cy(f"MATCH (e:Event {{namespace:'{NS_QQ}'}}) RETURN e.summary LIMIT 3000")
    summaries = [(r.get("row") or [""])[0] or "" for r in rows]
    sigs = [s for text in summaries for s in extract_signals(text)]
    cands = build_candidates(sigs, scope=NS_QQ)
    items = []
    for c in cands:
        gloss = getattr(c, "gloss", None)
        if not gloss or len(str(gloss)) < 2:
            continue
        ctx = [s for s in summaries if getattr(c, "term", "") in s][:2]
        items.append({
            "kind": "sense",
            "id": f"sense-{len(items)+1}",
            "term": getattr(c, "term", ""),
            "gloss": str(gloss)[:80],
            "score": getattr(c, "score", 0),
            "evidence": ctx,
            "scope": getattr(c, "scope", NS_QQ),
            "source": "reference",
        })
        if len(items) >= limit:
            break
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "labeling-data.js"))
    args = ap.parse_args()

    data = {
        "generated_at": "2026-11-13",
        "instructions": {
            "statement": "判断这句话是否真的表达了「谁负责/维护/依赖什么」；"
                         "machine 是规则抽取结果，抽错也算错。",
            "behavior": "判断这个人对目标的动作是否构成**稳定关系**"
                        "（≥2 次且像长期职责），偶发的算「偶发」。",
            "sense": "判断这个词在群里是否真的指这个意思；"
                     "上下文不足以判断时选「不确定」。",
        },
        "statements": build_statements(),
        "behaviors": build_behaviors(),
        "senses": build_senses(),
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.LABEL_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n",
                   encoding="utf-8")
    print(f"statements={len(data['statements'])} behaviors={len(data['behaviors'])} "
          f"senses={len(data['senses'])} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
