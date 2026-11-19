#!/usr/bin/env python3
"""把群头衔写进图（spec 047 T6）。

数据来源：`back_end/data/corpus/qq-identities/group-<gid>.json` 的 `title` 字段
（`harness/export_qq_identities.py` 从 `group_member3.64023` 导出的）。

用法：

    python3 harness/apply_titles.py --namespace qq-10000000001            # dry-run
    python3 harness/apply_titles.py --namespace qq-10000000001 --apply    # 真写
    python3 harness/apply_titles.py --namespace qq-10000000001 --apply --limit-groups 20
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.identity import ensure_titles  # noqa: E402

ID_DIR = ROOT / "back_end" / "data" / "corpus" / "qq-identities"


def main() -> int:
    ap = argparse.ArgumentParser(description="群头衔写图（spec 047 T6）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--limit-groups", type=int, default=0, help="只处理前 N 个群")
    args = ap.parse_args()

    settings = load_settings()
    files = sorted(ID_DIR.glob("group-*.json"))
    if args.limit_groups:
        files = files[: args.limit_groups]
    if not files:
        print(f"没有身份文件：{ID_DIR}")
        return 1

    total = wrote = skipped = 0
    groups = 0
    for f in files:
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {f.name}: {exc}")
            continue
        gid = f.stem.replace("group-", "")
        records = [
            {"uid": m.get("uid"), "title": m.get("title"), "source": "member_table"}
            for m in members if m.get("title")
        ]
        if not records:
            continue
        groups += 1
        total += len(records)
        if args.apply:
            stats = ensure_titles(
                namespace=args.namespace, scope=f"group:{gid}",
                records=records, settings=settings)
            wrote += stats["written"]
            skipped += stats["failed"]
            print(f"  ✓ {gid}: {len(records)} 条头衔 → 写 {stats['written']}")
        else:
            print(f"  · {gid}: {len(records)} 条头衔")

    print(f"\n涉及 {groups} 个群 / {total} 条头衔"
          + (f"；已写 {wrote}（失败 {skipped}）" if args.apply else "（dry-run）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
