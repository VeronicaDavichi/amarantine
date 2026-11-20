#!/usr/bin/env python3
"""校验文档里提到的 harness 命令**真实存在**（spec 049 A2 的一部分）。

## 为什么需要

早期 `docs/quickstart.md` 写过 `POST /admin/remember` —— 接口根本不存在，
用户照抄会失败。文档里出现的脚本路径/接口名必须能被机械核对，
否则「照着文档做一遍」这种验收会一直靠人肉。

本脚本做两件事：

1. 扫 `docs/*.md` 与 README，提取 `harness/xxx.py|mjs|sh` 路径，检查文件存在
2. `--run`：逐个跑 `--help`（或 `--list`），确认能启动、不自爆

用法：

    python3 harness/check_doc_cmds.py            # 只查存在性（CI 用，秒级）
    python3 harness/check_doc_cmds.py --run      # 顺带跑 --help
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 扫描的文档
DOCS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").glob("*.md")),
)

#: `harness/foo.py` 这类引用（反引号里也算）
_CMD_RE = re.compile(r"(harness/[A-Za-z0-9_\-]+\.(?:py|mjs|sh))")

#: `--help` 不安全/不支持的脚本（会真干活），只查存在性
NO_HELP = frozenset()

#: 这些脚本 `--help` 之外更适合用只读参数
SAFE_ARG = {
    "harness/purge_namespace.py": ["--list"],
    "harness/clean_qq_plain.py": ["--show-dir"],
    "harness/show_env.mjs": [],
}


def referenced() -> dict[str, list[str]]:
    """`{相对路径: [出现在哪些文档]}`。"""
    found: dict[str, list[str]] = {}
    for doc in DOCS:
        if not doc.is_file():
            continue
        for m in _CMD_RE.finditer(doc.read_text(encoding="utf-8")):
            found.setdefault(m.group(1), []).append(doc.name)
    return found


def run_one(rel: str) -> tuple[bool, str]:
    path = ROOT / rel
    args = SAFE_ARG.get(rel, ["--help"])
    if rel.endswith(".mjs"):
        cmd = ["node", str(path), *args]
    elif rel.endswith(".sh"):
        return True, "（shell 脚本，跳过执行）"
    else:
        venv = ROOT / "back_end" / ".venv-linux" / "bin" / "python3"
        py = str(venv) if venv.is_file() else sys.executable
        cmd = [py, str(path), *args]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "超时（120s）"
    if r.returncode == 0:
        return True, "ok"
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
    return False, f"exit={r.returncode} {tail[0][:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="校验文档里的 harness 命令存在/可启动")
    ap.add_argument("--run", action="store_true", help="顺带跑 --help")
    args = ap.parse_args()

    refs = referenced()
    missing = [rel for rel in refs if not (ROOT / rel).is_file()]
    print(f"文档引用 harness 脚本 {len(refs)} 个")
    for rel, docs in sorted(refs.items()):
        mark = "✓" if (ROOT / rel).is_file() else "✗"
        print(f"  {mark} {rel:<44} 见于 {', '.join(sorted(set(docs)))[:60]}")

    if missing:
        print(f"\n✗ 文档提到但文件不存在：{missing}")
        return 1

    # 反向：仓库里有但文档没提的脚本（只提示，不失败）
    on_disk = {str(p.relative_to(ROOT)) for p in (ROOT / "harness").glob("*")
               if p.suffix in (".py", ".mjs", ".sh")}
    undocumented = sorted(on_disk - set(refs))
    if undocumented:
        print(f"\n· 仓库里有但文档未提及（仅提示）：{len(undocumented)} 个")
        for rel in undocumented[:12]:
            print(f"    {rel}")

    if not args.run:
        print(f"\nOK: 文档引用的 {len(refs)} 个 harness 脚本全部存在"
              "（加 --run 可顺带跑 --help）")
        return 0

    print("\n=== 逐个 --help ===")
    failed = []
    for rel in sorted(refs):
        ok, detail = run_one(rel)
        print(f"  {'✓' if ok else '✗'} {rel:<44} {detail}")
        if not ok:
            failed.append(rel)
    if failed:
        print(f"\n✗ 启动失败：{failed}")
        return 1
    print(f"\nOK: 文档引用的 {len(refs)} 个 harness 脚本全部可启动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
