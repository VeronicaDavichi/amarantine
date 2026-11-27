#!/usr/bin/env python3
"""诊断：用 summary 的 4-gram 反查这张卡实际对应哪几条原文（不写库）。

## 为什么要修存量

`quotes_for_event` 修好了新写入，但 4383 张存量卡仍然挂着整个 30 条窗口。
`source_indices` 当时没存下来，所以无法直接重建 —— 但**摘要文本还在**，
可以用 4-gram 匹配同 session 的原文，找出真正相关的几条。

（这与 `_event_time` 的兜底是同一个手法，不是新发明的启发式。）

## 判据

给出每张卡：当前挂几条 / 匹配到几条 / 匹配到的原文前 40 字。
先看质量，再决定要不要写库。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

NS_DEFAULT = "qq-505456846"


def _bgrams(text: str, n: int = 4) -> set[str]:
    s = "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default=NS_DEFAULT)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-score", type=float, default=0.3,
                    help="4-gram 重合度阈值（0-1）")
    ap.add_argument("--json", default="", help="把匹配结果写到文件（供 apply 用）")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project

    s = load_settings_from_project()
    base = str(s.WEAVIATE_URL).rstrip("/")

    def gql(q: str) -> dict:
        req = urllib.request.Request(
            f"{base}/v1/graphql", data=json.dumps({"query": q}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=300).read())

    # 原文（按 session 分组，用于限定匹配范围）
    from memory.services.raw_quote import db_path_from_settings

    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    by_session: dict[str, list[tuple[str, str]]] = {}
    for qid, text, sid in con.execute(
            "SELECT quote_id, text, session_id FROM raw_quote WHERE namespace=?",
            (args.namespace,)):
        by_session.setdefault(str(sid or ""), []).append((qid, text or ""))
    con.close()
    print(f"原文 {sum(len(v) for v in by_session.values())} 条 / "
          f"{len(by_session)} 个 session")

    # 卡
    cards: list[dict] = []
    offset = 0
    while True:
        d = gql('{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
                'valueText: "' + args.namespace + '"}, limit: 500, offset: '
                + str(offset) + ') { event_id summary session_id raw_quote_ids } } }')
        rows = ((d.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
        if not rows:
            break
        cards.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
    print(f"卡片 {len(cards)} 张\n")

    plan: list[dict] = []
    stats = {"matched": 0, "fallback": 0, "cur_total": 0, "new_total": 0}
    for c in cards:
        cur = c.get("raw_quote_ids") or []
        stats["cur_total"] += len(cur)
        summary = c.get("summary") or ""
        pool = by_session.get(str(c.get("session_id") or ""), [])
        if not pool:
            pool = [x for v in by_session.values() for x in v]
        sg = _bgrams(summary)
        scored = []
        if sg:
            for qid, text in pool:
                tg = _bgrams(text)
                if not tg:
                    continue
                overlap = len(sg & tg) / len(sg)
                if overlap >= args.min_score:
                    scored.append((overlap, qid))
        scored.sort(key=lambda x: -x[0])
        picked = [q for _sc, q in scored[:10]]
        if picked:
            stats["matched"] += 1
            stats["new_total"] += len(picked)
            plan.append({"event_id": c["event_id"], "summary": summary,
                         "before": cur, "after": picked,
                         "score": scored[0][0] if scored else 0.0})
        else:
            stats["fallback"] += 1
            stats["new_total"] += len(cur)
            plan.append({"event_id": c["event_id"], "summary": summary,
                         "before": cur, "after": cur, "score": 0.0})

    print(f"{'指标':<18}{'值':>10}")
    print(f"{'匹配成功':<18}{stats['matched']:>10}")
    print(f"{'回退整批':<18}{stats['fallback']:>10}")
    print(f"{'当前总挂载':<18}{stats['cur_total']:>10}")
    print(f"{'修复后总挂载':<18}{stats['new_total']:>10}")
    print(f"{'平均每卡（前）':<18}{stats['cur_total']/max(1,len(cards)):>10.1f}")
    print(f"{'平均每卡（后）':<18}{stats['new_total']/max(1,len(cards)):>10.1f}")

    print(f"\n前 {args.limit} 张抽样（看匹配质量）：")
    for p in plan[: args.limit]:
        print(f"\n  {p['event_id']}  score={p['score']:.2f}  "
              f"{len(p['before'])} → {len(p['after'])} 条")
        print(f"    摘要: {p['summary'][:60]}")
        for q in p["after"][:3]:
            txt = next((t for v in by_session.values() for qq, t in v if qq == q), "")
            print(f"      命中: {txt[:50]}")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps({"namespace": args.namespace, "plan": plan,
                        "stats": stats}, ensure_ascii=False), encoding="utf-8")
        print(f"\n计划已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
