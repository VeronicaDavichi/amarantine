#!/usr/bin/env python3
"""从 emoji 昵称推导别名，用**真实语料**验证后写进图（spec 047 延伸）。

思路（用户 2026-11-18）：群里常按 emoji 的意思叫人 —— `🦆🍗` 会被叫「鸭腿」。

做法：

1. 遍历成员表里含 emoji 的名字，推导候选（去 emoji / emoji 替词）
2. 数候选在 `raw_quote` 里出现多少次（**这就是证据**）
3. 达到 `--min-hits` 才写 `(:Account)-[:DISPLAYED_AS {source:'emoji_alias'}]->(:Alias)`

为什么必须验证：替词容易臆造 —— `便携式🦆🍗 → 便携式鸭腿` 在 4383 张卡里
一次都没出现过；而 `便携式` 有 139 次。没有这道闸，就会批量造出假别名。

用法：

    python3 harness/emoji_alias_scan.py --namespace qq-505456846            # 报告
    python3 harness/emoji_alias_scan.py --namespace qq-505456846 --apply
    python3 harness/emoji_alias_scan.py --namespace qq-505456846 --min-hits 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.emoji_alias import derive_names, has_emoji  # noqa: E402

ID_DIR = ROOT / "back_end" / "data" / "corpus" / "qq-identities"
DB = ROOT / "back_end" / "data" / "memory.db"


def load_members() -> dict[str, dict]:
    """`{name: {uid, card, nickname}}`（含 emoji 的才要）。"""
    out: dict[str, dict] = {}
    for f in sorted(ID_DIR.glob("group-*.json")):
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception:  # noqa: BLE001
            continue
        for m in members:
            uid = str(m.get("uid") or "").strip()
            if not uid:
                continue
            for key in ("card", "nickname"):
                name = str(m.get(key) or "").strip()
                if name and has_emoji(name):
                    out.setdefault(name, {"uid": uid, "card": m.get("card"),
                                          "nickname": m.get("nickname")})
    return out


def corpus_hits(con: sqlite3.Connection, namespace: str, term: str) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM raw_quote WHERE namespace=? AND text LIKE ?",
        (namespace, f"%{term}%")).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="emoji 昵称 → 别名（语料验证）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--min-hits", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个候选（调试）")
    args = ap.parse_args()

    members = load_members()
    print(f"含 emoji 的名字 {len(members)} 个")

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows: list[tuple[str, str, str, str, int]] = []
    for name, info in members.items():
        for cand, how in derive_names(name):
            hits = corpus_hits(con, args.namespace, cand)
            if hits >= args.min_hits:
                rows.append((name, cand, how, info["uid"], hits))
    con.close()

    rows.sort(key=lambda r: -r[4])
    if args.limit:
        rows = rows[: args.limit]
    print(f"过闸（语料 ≥{args.min_hits} 次）的候选 {len(rows)} 条\n")
    for name, cand, how, uid, hits in rows[:40]:
        print(f"  {name!r:<34} → {cand!r:<16} [{how}] 语料 {hits} 次")

    if not args.apply:
        print("\n（dry-run；加 --apply 才写图）")
        return 0

    from memory.config import load_settings
    from memory.services.identity import ensure_display_names

    settings = load_settings()
    records = [{"uid": uid, "name": cand, "source": "emoji_alias"}
               for _name, cand, _how, uid, _hits in rows]
    stats = ensure_display_names(namespace=args.namespace, scope="global",
                                records=records, settings=settings)
    print(f"\n写入 global 别名 {stats['written']} 条（失败 {stats['failed']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
