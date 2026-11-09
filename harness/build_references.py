#!/usr/bin/env python3
"""指称层建候选（spec 041 M1 T3）：扫会话原文 → 触发信号 → Sense 候选。

用法：

    # 只看候选（默认）
    python ../harness/build_references.py --namespace njzq-dingtalk

    # 真写
    python ../harness/build_references.py --namespace njzq-dingtalk --apply

    # 只扫某个会话
    python ../harness/build_references.py --namespace njzq-dingtalk --session 78497500999

## 输入

- `raw_quote`：每个会话的原文（带 `session_id` / `ts` / `quote_id`）
- `Account.nick`：群内账号昵称（「同名账号」信号）

## 输出

`Term` / `Sense` 节点。`Sense.status` 由分数决定：
≥2 `uncertain`、≥4 `probable`；有食物/动作语境的反向信号会压分。
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.reference import (  # noqa: E402
    build_candidates,
    extract_signals,
)


def load_quotes(db_path: pathlib.Path, session: str | None) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = "SELECT quote_id, text, session_id, ts FROM raw_quote"
    args: tuple = ()
    if session:
        sql += " WHERE session_id = ?"
        args = (session,)
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return rows


def load_nicks(driver, namespace: str) -> list[str]:
    with driver.session() as session:
        return [r["nick"] for r in session.run(
            "MATCH (a:Account {namespace:$ns}) RETURN a.nick AS nick", ns=namespace
        ) if r["nick"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="指称层建候选（spec 041）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--session", default="", help="只扫这个 session_id")
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--min-score", type=int, default=2, help="候选阈值（默认 2）")
    ap.add_argument("--limit", type=int, default=30, help="最多打印多少条候选")
    args = ap.parse_args()

    from memory.config import load_settings
    from neo4j import GraphDatabase

    settings = load_settings()
    db_path = pathlib.Path(settings.DATABASE_URL.replace("sqlite:///", ""))
    if not db_path.is_absolute():
        db_path = ROOT / "back_end" / db_path

    quotes = load_quotes(db_path, args.session or None)
    print(f"原文 {len(quotes)} 条（session={args.session or '全部'}）")

    driver = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    try:
        nicks = load_nicks(driver, args.namespace)
        print(f"账号昵称 {len(nicks)} 个")

        by_scope: dict[str, list] = {}
        for q in quotes:
            scope = f"session:{q.get('session_id') or 'unknown'}"
            sigs = extract_signals(q["text"], quote_id=q["quote_id"], ts=q.get("ts"))
            if sigs:
                by_scope.setdefault(scope, []).extend(sigs)

        total = 0
        all_candidates = []
        for scope, sigs in by_scope.items():
            cands = build_candidates(sigs, scope=scope, nicks=nicks,
                                     candidate_min=args.min_score)
            all_candidates.extend(cands)
            total += len(cands)

        all_candidates.sort(key=lambda c: (-c.score, c.term))
        print(f"\n候选义项 {total} 个（阈值 {args.min_score}）")
        for c in all_candidates[:args.limit]:
            gloss = f" gloss={c.gloss}" if c.gloss else ""
            print(f"  [{c.score:>2}] {c.term}{gloss}  scope={c.scope}  "
                  f"status={c.status} conf={c.confidence} evidence={len(c.evidence)}")
        if total > args.limit:
            print(f"  …还有 {total - args.limit} 条")

        if not args.apply:
            print("\n（dry-run；加 --apply 才写 Term/Sense）")
            return 0

        now = datetime.now(timezone.utc).isoformat()
        written = 0
        by_scope_items: dict[str, list[dict]] = {}
        for c in all_candidates:
            by_scope_items.setdefault(c.scope, []).append({
                "term": c.term, "scope": c.scope, "gloss": c.gloss or "",
                "status": c.status, "confidence": c.confidence,
                "evidence": c.evidence[-20:],
            })
        from memory.services.reference import MERGE_SENSE_CYPHER

        with driver.session() as session:
            for scope, items in by_scope_items.items():
                res = session.run(MERGE_SENSE_CYPHER, items=items, now=now).single()
                written += int(res["n"]) if res else 0
        print(f"\n写入 Sense {written} 个")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
