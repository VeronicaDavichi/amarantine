#!/usr/bin/env python3
"""写 Claim 与 Mention（spec 041 M2）。

用法：

    python ../harness/apply_claims.py --namespace njzq-dingtalk          # 只看
    python ../harness/apply_claims.py --namespace njzq-dingtalk --apply  # 真写

做什么：

1. 扫 `raw_quote`，抽 `assert / question / deny` 三类断言
2. 对已存在的 `Sense`（同 term/scope/gloss）裁决状态并写 `Claim`
3. 顺带写 `Mention`（`(:Mention)-[:USES_SENSE]->(:Sense)`）

`question` 只写 `Claim`，**不改状态、不建 `USES_SENSE`**（REQ-4）。
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
    APPLY_CLAIM_CYPHER,
    MERGE_MENTION_CYPHER,
    extract_claims,
    resolve_status,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="写 Claim / Mention（spec 041 M2）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from memory.config import load_settings
    from neo4j import GraphDatabase

    settings = load_settings()
    db = pathlib.Path(settings.DATABASE_URL.replace("sqlite:///", ""))
    if not db.is_absolute():
        db = ROOT / "back_end" / db
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    quotes = [dict(r) for r in con.execute(
        "SELECT quote_id, text, session_id, ts FROM raw_quote")]
    con.close()

    claims: list = []
    for q in quotes:
        scope = f"session:{q.get('session_id') or 'unknown'}"
        claims.extend(extract_claims(q["text"], scope=scope, quote_id=q["quote_id"], ts=q.get("ts")))
    print(f"原文 {len(quotes)} 条 → 断言 {len(claims)} 条"
          f"（assert {sum(1 for c in claims if c.mode == 'assert')} / "
          f"question {sum(1 for c in claims if c.mode == 'question')} / "
          f"deny {sum(1 for c in claims if c.mode == 'deny')}）")

    driver = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
    )
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    try:
        with driver.session() as session:
            existing = {
                (r["term"], r["scope"], r["gloss"]): r["status"]
                for r in session.run(
                    f"MATCH (s:Sense) WHERE s.scope STARTS WITH 'session:' "
                    f"RETURN s.term AS term, s.scope AS scope, s.gloss AS gloss, s.status AS status"
                )
            }
            grouped: dict[tuple[str, str, str], list] = {}
            for c in claims:
                key = (c.term, c.scope, c.gloss or "")
                if key not in existing:
                    continue  # 没有对应 Sense 的断言先不落（避免建孤立 Claim）
                grouped.setdefault(key, []).append(c)

            print(f"能落到已有 Sense 的断言：{sum(len(v) for v in grouped.values())} 条")
            for (term, scope, gloss), items in grouped.items():
                status = resolve_status(existing[(term, scope, gloss)], items)
                payload = [{
                    "term": term, "scope": scope, "gloss": gloss,
                    "mode": i.mode, "quote_id": i.quote_id, "ts": i.ts,
                    "speaker": i.speaker, "status": status,
                } for i in items]
                if args.apply:
                    session.run(APPLY_CLAIM_CYPHER, items=payload, now=now).consume()
                    # Mention：只给 assert/deny 建（question 不建边）
                    mentions = [{
                        "term": term, "scope": scope, "gloss": gloss,
                        "quote_id": i.quote_id, "ts": i.ts,
                    } for i in items if i.mode in ("assert", "deny") and i.quote_id]
                    if mentions:
                        session.run(MERGE_MENTION_CYPHER, items=mentions, now=now).consume()
                written += 1
                print(f"  {term} @ {scope} gloss={gloss!r} → {status}（{len(items)} 条断言）")

        if not args.apply:
            print("\n（dry-run；加 --apply 才写）")
        else:
            print(f"\n处理 Sense {written} 个")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
