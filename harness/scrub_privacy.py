# -*- coding: utf-8 -*-
"""开源前脱敏：把真实姓名 / IP / QQ 号替换成既有映射里的假值。

## 为什么要有这一步

开源发布意味着这些字符串会永久进入公开历史。仓库里目前仍有：

- QQ 号 `505456846`（出现在 harness 默认参数与文档示例里）
- 内网 IP `192.168.0.58`（LLM 网关）、`172.20.192.1`（WSL 宿主）
- 真实姓名（孙柏林 / 任泽昂 / 王婕）

## 映射来源

沿用项目里**已经存在**的脱敏映射，避免造出第三套假名：
`孙柏林→陈柏林`、`任泽昂→任泽安`、`王婕→王洁`、
`192.168.0.58→198.51.100.58`、`172.20.192.1→198.51.100.1`、
`505456846→10000000001`。

## 范围

只改**被 git 跟踪的文本文件**；跳过 `data/`、`corpus/`、`backups/`、
`.venv*`、`.git`、日志。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/mnt/e/PycharmProjects/amarantine")

MAP = {
    "505456846": "10000000001",
    "192.168.0.58": "198.51.100.58",
    "172.20.192.1": "198.51.100.1",
    "孙柏林": "陈柏林",
    "任泽昂": "任泽安",
    "王婕": "王洁",
}

SKIP_DIRS = ("data/", "corpus/", "backups/", ".venv", ".git/", "node_modules/")
SKIP_SUFFIX = (".log", ".db", ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".xlsx")


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
    apply = "--apply" in sys.argv
    hits: dict[str, int] = {k: 0 for k in MAP}
    files_touched: dict[str, int] = {}

    for p in tracked_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        orig = text
        changed = 0
        for real, fake in MAP.items():
            if real in text:
                n = text.count(real)
                hits[real] += n
                text = text.replace(real, fake)
                changed += n
        if changed and text != orig:
            files_touched[str(p.relative_to(ROOT))] = changed
            if apply:
                p.write_text(text, encoding="utf-8")

    print(f"{'替换' if apply else '将替换'} {sum(files_touched.values())} 处，"
          f"涉及 {len(files_touched)} 个文件\n")
    for real, n in hits.items():
        if n:
            print(f"  {real!r:<18} × {n}")
    print()
    for rel, n in sorted(files_touched.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {rel:<52} {n}")
    if not apply:
        print("\n（dry-run；加 --apply 才写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
