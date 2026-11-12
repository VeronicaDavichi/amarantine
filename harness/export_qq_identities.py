#!/usr/bin/env python3
"""从解密后的 group_info.db 导出成员身份（含**群头衔**，spec 047）。

## 为什么不用 MCP 的 list_group_members

MCP 只返回 uid/qq/昵称/群名片，**不返回群头衔**。而 QQ 的群头衔在
`group_info.db.group_member3` 的 `64023` 列（protobuf 字段编号式列名）。

## 列名对照（实测群 485490399，390 个成员）

| 列 | 含义 | 例子 |
|---|---|---|
| `60001` | 群号 | `485490399` |
| `1000` | NT uid | `u_6IiO6q6eeK5pHe6d1eP_Mw` |
| `1002` | QQ 号 | `2742830098` |
| `20002` | 昵称 | `铭旌` |
| `64003` | 群名片 | `羽生家的大肥鱼` |
| `64023` | **群头衔** | `机仆` |

## 用法

    # 1) 先解密（qq-mcp 工具，会打印 plaintext 路径）
    node harness/export_qq_titles.mjs
    # 2) 再导出
    python3 harness/export_qq_identities.py --plain <plaintext 路径> \
        --out-dir back_end/data/corpus/qq-identities
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

COL_GROUP = "60001"
COL_UID = "1000"
COL_QQ = "1002"
COL_NICK = "20002"
COL_CARD = "64003"
COL_TITLE = "64023"


def export(plain: pathlib.Path, out_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(plain)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        f'SELECT "{COL_GROUP}" AS gid, "{COL_UID}" AS uid, "{COL_QQ}" AS qq, '
        f'"{COL_NICK}" AS nickname, "{COL_CARD}" AS card, "{COL_TITLE}" AS title '
        "FROM group_member3"
    ))
    con.close()

    by_group: dict[str, list[dict]] = {}
    for r in rows:
        gid = str(r["gid"] or "").strip()
        if not gid:
            continue
        by_group.setdefault(gid, []).append({
            "uid": r["uid"],
            "qq": r["qq"],
            "nickname": r["nickname"] or "",
            "card": r["card"] or "",
            "title": r["title"] or "",
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"groups": 0, "members": 0, "titled": 0}
    for gid, members in by_group.items():
        path = out_dir / f"group-{gid}.json"
        path.write_text(
            json.dumps({"groupQq": gid, "members": members}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        stats["groups"] += 1
        stats["members"] += len(members)
        stats["titled"] += sum(1 for m in members if m["title"])
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="从解密后的 group_info.db 导出成员身份")
    ap.add_argument("--plain", required=True, help="解密后的 group_info.db 路径")
    ap.add_argument(
        "--out-dir",
        default=str(pathlib.Path(__file__).resolve().parents[1]
                    / "back_end" / "data" / "corpus" / "qq-identities"),
    )
    args = ap.parse_args()
    stats = export(pathlib.Path(args.plain), pathlib.Path(args.out_dir))
    print(f"导出 {stats['groups']} 个群 / {stats['members']} 个成员"
          f"（其中 {stats['titled']} 人有群头衔）→ {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
