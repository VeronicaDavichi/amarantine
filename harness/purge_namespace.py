#!/usr/bin/env python3
"""删掉某个 namespace 的三处数据（spec 050 的 CLI 形态）。

为什么还要 CLI：容器里/脚本里没有 HTTP 也能用；而且它天然适合放进
「不想让管理接口暴露」的环境。

用法：

    # 先看有什么
    python3 harness/purge_namespace.py --list

    # 预演（只数不改）
    python3 harness/purge_namespace.py --namespace verify046b

    # 真删（确认语必须一字不差）
    python3 harness/purge_namespace.py --namespace verify046b \\
        --confirm "purge verify046b" --apply

删除顺序固定：图 → 卡片 → SQLite（先拆派生层，再删事实源）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description="清理某个 namespace 的记忆数据")
    ap.add_argument("--namespace", default="")
    ap.add_argument("--confirm", default="", help='必须是 "purge <namespace>"')
    ap.add_argument("--apply", action="store_true", help="真删（默认预演）")
    ap.add_argument("--list", action="store_true", help="列出所有 namespace")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from memory.config import load_settings
    from memory.services.purge import (
        PurgeError,
        list_namespaces,
        purge_namespace,
    )

    settings = load_settings()

    if args.list:
        rows = list_namespaces(settings)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print(f"{'namespace':<24}{'卡片':>8}")
            for r in sorted(rows, key=lambda x: -x["cards"]):
                print(f"{r['namespace']:<24}{r['cards']:>8}")
        return 0

    if not args.namespace:
        print("需要 --namespace（或用 --list 看有哪些）")
        return 2

    try:
        report = purge_namespace(
            settings, args.namespace, confirm=args.confirm, apply=args.apply)
    except PurgeError as exc:
        print(f"✗ {exc}")
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["ok"] else 1

    print(f"namespace: {report['namespace']}")
    print(f"模式: {'真删' if report['applied'] else '预演（加 --apply 才删）'}")
    print("\n将删/已删：")
    for k, v in report["before"].items():
        done = report["deleted"].get(k, 0)
        print(f"  {k:<20} 现有 {v:>7}   删了 {done:>7}")
    if report["applied"]:
        residual = report.get("residual") or {}
        print("\n删后复核：", "干净 ✓" if report["ok"] else f"仍有残留 ✗ {residual}")

    print("\n提示：qq-mcp 的解密副本（~/.qq-mcp/plain/）不归本项目管，"
          "如需要请自行 `rm -rf ~/.qq-mcp/plain`。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
