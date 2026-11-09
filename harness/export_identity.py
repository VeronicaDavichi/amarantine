#!/usr/bin/env python3
"""spec 043 T1：从钉钉本地库导出「身份记录」，供灌数据时当权威名册用。

数据来源（实测）：

| 表 | 行数 | 字段 | 状态 |
|---|---|---|---|
| `tbuser_profile_v2` | 469 | `uid`, `nick`, `alias` | 昵称可用；`alias` 全空 |
| `tbuser_alias_name` | 1 | `uid`, `alias` | 备注样本：`王晓虹 - 加班问题人事` |
| `contact_friend` | **0** | `remark` | 表在、数据空 → **备注当前拿不到** |

输出：`back_end/data/corpus/identity.json`

```json
{"_note": "...", "identities": [{"uid": "...", "nick": "...", "remark": "...", ...}]}
```

用法：

    python ../harness/export_identity.py                 # 用默认路径
    python ../harness/export_identity.py --db <path>     # 指定库
    python ../harness/export_identity.py --out <path>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.identity import Identity, is_non_han  # noqa: E402

DEFAULT_DB = Path.home() / ".dingtalk_mcp" / "decrypted" / "dingtalk.db"
DEFAULT_OUT = ROOT / "back_end" / "data" / "corpus" / "identity.json"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _rows(conn: sqlite3.Connection, table: str, columns: set[str], wanted: tuple[str, ...]) -> list[dict]:
    if not columns or not set(wanted) <= columns:
        return []
    try:
        cur = conn.execute(f"SELECT {', '.join(wanted)} FROM {table}")
    except sqlite3.Error:
        return []
    return [dict(zip(wanted, row)) for row in cur]


def export(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        profiles = _rows(conn, "tbuser_profile_v2", _table_columns(conn, "tbuser_profile_v2"),
                         ("uid", "nick", "alias"))
        remarks = _rows(conn, "tbuser_alias_name", _table_columns(conn, "tbuser_alias_name"),
                        ("uid", "alias"))
        remark_cols = _table_columns(conn, "contact_friend")
        remark_rows = _rows(conn, "contact_friend", remark_cols, ("uid", "remark"))
    finally:
        conn.close()

    remark_by_uid: dict[str, str] = {}
    for row in remark_rows:  # contact_friend 当前 0 行，留接口
        if str(row.get("remark") or "").strip():
            remark_by_uid[str(row["uid"])] = str(row["remark"]).strip()
    for row in remarks:  # tbuser_alias_name：别名表
        alias = str(row.get("alias") or "").strip()
        if alias:
            remark_by_uid.setdefault(str(row["uid"]), alias)

    identities: list[dict] = []
    seen: set[str] = set()
    for row in profiles:
        uid = str(row.get("uid") or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        ident = Identity(
            uid=uid,
            nick=str(row.get("nick") or ""),
            remark=remark_by_uid.get(uid, ""),
            source="tbuser_profile_v2",
        )
        if not ident.display_name:
            continue
        identities.append({
            "uid": ident.uid,
            "nick": ident.nick,
            "remark": ident.remark,
            "display_name": ident.display_name,
            "non_han": is_non_han(ident.display_name),
            "source": ident.source,
        })
    return {
        "_note": "spec 043 T1：身份记录。备注（contact_friend.remark）当前为空，"
                 "仅 tbuser_alias_name 有 1 条样本。",
        "_source": "DingTalk 本地库 tbuser_profile_v2 + tbuser_alias_name",
        "identities": identities,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="导出钉钉身份记录（spec 043 T1）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"✗ 找不到钉钉库: {db}")
        print("  用 --db 指定，或确认 .dingtalk_mcp/decrypted/dingtalk.db 存在")
        return 1

    data = export(db)
    rows = data["identities"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    non_han = [r for r in rows if r["non_han"]]
    with_remark = [r for r in rows if r["remark"]]
    print(f"身份记录 {len(rows)} 条 → {out}")
    print(f"  带备注: {len(with_remark)} | 非汉族姓名(已标): {len(non_han)}")
    if non_han:
        print("  非汉族样例:", [r["display_name"] for r in non_han[:5]])
    print("  备注字段状态: contact_friend 为空表 → 备注实际不可得（见 spec 043 design）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
