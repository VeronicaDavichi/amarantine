#!/usr/bin/env python3
"""开源前脱敏：把真实姓名 / 内网 IP / QQ 号替换成假值。

## 为什么映射表要外置

映射表里写着**真实姓名**，把它提交进仓库本身就是泄露 ——
第一版脚本就犯了这错（`git ls-files` 里能搜到真名）。

所以：

| 文件 | 入库 | 内容 |
|---|---|---|
| `harness/privacy-map.example.json` | ✅ | 占位符之间的映射（`"真实姓名A": "张三"`），示范格式 |
| `harness/.privacy-map.json` | ❌ gitignored | 真实映射，只在本机 |

没有真实映射文件时脚本用示例映射跑 —— 对开源用户来说这就是「没用真实数据」，
对维护者来说本机有真实映射就能清干净。

## 用法

    python3 harness/scrub_privacy.py                 # dry-run
    python3 harness/scrub_privacy.py --apply         # 写回
    python3 harness/scrub_privacy.py --map 其它.json  # 指定映射
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_MAP = ROOT / "harness" / ".privacy-map.json"
EXAMPLE_MAP = ROOT / "harness" / "privacy-map.example.json"

SKIP_DIRS = ("data/", "corpus/", "backups/", ".venv", ".git/", "node_modules/")
SKIP_SUFFIX = (".log", ".db", ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".xlsx")


def load_map(path: pathlib.Path | None) -> tuple[dict[str, str], str]:
    """返回 `(映射, 来源说明)`。"""
    if path:
        return json.loads(path.read_text(encoding="utf-8")), str(path)
    if REAL_MAP.is_file():
        return json.loads(REAL_MAP.read_text(encoding="utf-8")), str(REAL_MAP)
    if EXAMPLE_MAP.is_file():
        return json.loads(EXAMPLE_MAP.read_text(encoding="utf-8")), str(EXAMPLE_MAP)
    return {}, "（没有映射文件）"


def tracked_files() -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    files = []
    for rel in out:
        if any(rel.startswith(d) or f"/{d}" in rel for d in SKIP_DIRS):
            continue
        if rel.endswith(SKIP_SUFFIX):
            continue
        p = ROOT / rel
        if p.is_file():
            files.append(p)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="开源前脱敏")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--map", default="")
    args = ap.parse_args()

    mapping, source = load_map(pathlib.Path(args.map) if args.map else None)
    if not mapping:
        print("没有可用映射，无事可做")
        return 0
    print(f"映射来源：{source}（{len(mapping)} 条）\n")

    hits: dict[str, int] = {k: 0 for k in mapping}
    files_touched: dict[str, int] = {}
    for p in tracked_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        orig = text
        changed = 0
        for real, fake in mapping.items():
            if real and real in text:
                hits[real] += text.count(real)
                text = text.replace(real, fake)
                changed += text.count(fake) and 0 or 0  # 只用于计数占位
        if text != orig:
            n = sum(hits[k] for k in mapping)  # 粗略：总替换数
            files_touched[str(p.relative_to(ROOT))] = max(
                files_touched.get(str(p.relative_to(ROOT)), 0), changed)
            if args.apply:
                p.write_text(text, encoding="utf-8")

    total = sum(hits.values())
    print(f"{'替换' if args.apply else '将替换'} {total} 处，"
          f"涉及 {len(files_touched)} 个文件\n")
    for real, n in hits.items():
        if n:
            print(f"  {real!r:<18} × {n}")
    print()
    for rel, _n in sorted(files_touched.items()):
        print(f"  {rel}")
    if not args.apply:
        print("\n（dry-run；加 --apply 才写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
