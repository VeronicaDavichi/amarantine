#!/usr/bin/env python3
"""把名字标记写进图（spec 047 T1–T3 的可重跑步骤）。

来源：`back_end/data/corpus/qq-identities/group-<gid>.json`
（`export_qq_identities.py` 从 `group_member3` 导出：uid/qq/昵称/群名片/头衔）

写两类 scope：

- `group:<gid>` ← 群名片（`source=group_card`），可用则用
- `global`      ← 账号昵称（`source=nick`）

为什么必须能重跑：清空 namespace（`MATCH (n {namespace}) DETACH DELETE n`）
会把 Account/Alias 一起删掉，名字标记全丢；重灌后要补这一步。

用法：

    python3 harness/apply_display_names.py --namespace qq-505456846            # dry-run
    python3 harness/apply_display_names.py --namespace qq-505456846 --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.identity import ensure_display_names  # noqa: E402
from memory.services.name_marker import classify_card  # noqa: E402

ID_DIR = ROOT / "back_end" / "data" / "corpus" / "qq-identities"


def usable(name: str) -> bool:
    return bool(name) and classify_card(name).usable


def main() -> int:
    ap = argparse.ArgumentParser(description="名字标记写图（spec 047）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit-groups", type=int, default=0)
    args = ap.parse_args()

    settings = load_settings()
    files = sorted(ID_DIR.glob("group-*.json"))
    if args.limit_groups:
        files = files[: args.limit_groups]
    if not files:
        print(f"没有身份文件：{ID_DIR}")
        return 1

    cards_written = nicks_written = 0
    for f in files:
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {f.name}: {exc}")
            continue
        gid = f.stem.replace("group-", "")
        card_records = [
            {"uid": m.get("uid"), "name": (m.get("card") or "").strip(),
             "source": "group_card"}
            for m in members
            if usable((m.get("card") or "").strip())
        ]
        nick_records = [
            {"uid": m.get("uid"), "name": (m.get("nickname") or "").strip(),
             "source": "nick"}
            for m in members
            if usable((m.get("nickname") or "").strip())
        ]
        if args.apply:
            if card_records:
                s1 = ensure_display_names(namespace=args.namespace,
                                          scope=f"group:{gid}",
                                          records=card_records, settings=settings)
                cards_written += s1["written"]
            if nick_records:
                s2 = ensure_display_names(namespace=args.namespace, scope="global",
                                          records=nick_records, settings=settings)
                nicks_written += s2["written"]
        print(f"  {'✓' if args.apply else '·'} {gid}: 名片 {len(card_records)} / "
              f"昵称 {len(nick_records)}")

    print(f"\n共 {len(files)} 个群"
          + (f"；写名片 {cards_written}、昵称 {nicks_written}" if args.apply
             else "（dry-run）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
