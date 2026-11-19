#!/usr/bin/env python3
"""清理 qq-mcp 的解密副本（`~/.qq-mcp/plain/`）。

## 为什么需要自动清理

qq-mcp 每次读库都会在 `~/.qq-mcp/plain/` 生成**明文**副本，命名规则：

```
<原库名>.<原文件字节数>.<毫秒时间戳>.plain.db
```

实测累积到 **3.3 GB**：同一库留多份、旧的不自动删。原库是加密的，
这些副本反而是**明文聊天记录** —— 比原库更敏感，导完就该删。

## 怎么保证删得安全

1. **只删这个目录下的文件**，且文件名必须匹配 `.plain.db` 系列后缀
   （含 `-wal` / `-shm` 尾巴），不碰目录本身、不碰 `qqkey`
2. 删除前确认**原库目录还在**（原库没了，解密副本就是唯一数据源，不能删）
3. `older_than` 默认 0（都删）；给个秒数可以避开「另一个会话正在用」

用法：

    python3 harness/clean_qq_plain.py                # 预演：列出会删什么
    python3 harness/clean_qq_plain.py --apply        # 真删
    python3 harness/clean_qq_plain.py --apply --older-than 600   # 只删 10 分钟前的
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

#: 解密副本的落盘目录（qq-mcp 的约定）。
#: **注意**：qq-mcp 是 node 进程，在 Windows 侧跑，所以它的 `~` 是
#: `C:\Users\<用户>`，不是 WSL 的 `/home/<用户>`。这里按顺序找：
#:   1. `QQ_MCP_PLAIN_DIR` 环境变量
#:   2. `~/.qq-mcp/plain`（原生 Linux/macOS）
#:   3. `/mnt/c/Users/*/.qq-mcp/plain`（WSL 里跑、qq-mcp 在 Windows）
PLAIN_DIR = pathlib.Path.home() / ".qq-mcp" / "plain"

#: 允许删的后缀。`.plain.db` 是主副本，`-wal`/`-shm` 是它的边角文件。
PLAIN_SUFFIXES = (".plain.db", ".plain.db-wal", ".plain.db-shm")

#: 原库目录（删副本前必须确认它还在）。可由 `QQ_NT_DB_DIR` 覆盖。
DEFAULT_SRC_DIR = pathlib.Path(
    "/mnt/d/TencentData/Chats/Tencent Files/10000000001/nt_qq/nt_db")


def _paths():
    """拿 `(plain 目录, 原库目录)` —— 解析逻辑在 services/local_paths。"""
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                           / "back_end" / "src"))
    from memory.services.local_paths import (
        load_settings_from_project,
        nt_db_dir,
        qq_plain_dir,
    )

    try:
        settings = load_settings_from_project()
    except Exception:  # noqa: BLE001 - 配置读不出来就退回环境变量/默认
        settings = None
    return qq_plain_dir(settings), (nt_db_dir(settings) or DEFAULT_SRC_DIR)


def find_plain_dir() -> pathlib.Path:
    return _paths()[0]


def source_dir() -> pathlib.Path:
    return _paths()[1]


def candidates(plain_dir: pathlib.Path, *, older_than_s: float = 0.0) -> list[pathlib.Path]:
    """列出可以删的解密副本。"""
    if not plain_dir.is_dir():
        return []
    cutoff = time.time() - older_than_s
    out: list[pathlib.Path] = []
    for p in plain_dir.iterdir():
        if not p.is_file() or not p.name.endswith(PLAIN_SUFFIXES):
            continue
        try:
            if p.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def clean(
    *,
    plain_dir: pathlib.Path | None = None,
    src_dir: pathlib.Path | None = None,
    older_than_s: float = 0.0,
    apply: bool = False,
) -> dict:
    """删除解密副本。

    Returns:
        `{"dir", "freed_bytes", "files", "skipped_reason"}`。

    Note:
        `src_dir` 不存在时**不删**：原库没了，副本就是唯一数据源。
    """
    plain = plain_dir or find_plain_dir()
    src = src_dir or source_dir()

    if not src.is_dir():
        return {"dir": str(plain), "freed_bytes": 0, "files": [],
                "skipped_reason": f"原库目录不存在（{src}），拒绝删除副本"}

    files = candidates(plain, older_than_s=older_than_s)
    freed = 0
    done: list[str] = []
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if apply:
            try:
                p.unlink()
                done.append(p.name)
                freed += size
            except OSError as exc:  # noqa: BLE001 - 删不掉就跳过，别中断
                print(f"  ⚠ 删不掉 {p.name}: {exc}", file=sys.stderr)
        else:
            done.append(p.name)
            freed += size
    return {"dir": str(plain), "freed_bytes": freed, "files": done,
            "applied": apply, "skipped_reason": ""}


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 qq-mcp 的解密副本")
    ap.add_argument("--apply", action="store_true", help="真删（默认预演）")
    ap.add_argument("--older-than", type=float, default=0.0,
                    help="只删 N 秒前的文件（默认 0 = 全删）")
    ap.add_argument("--dir", default="", help="覆盖 plain 目录（测试用）")
    ap.add_argument("--show-dir", action="store_true", help="只打印定位到的目录")
    ap.add_argument("--src", default="", help="覆盖原库目录（测试用）")
    args = ap.parse_args()

    if args.show_dir:
        print(f"plain 目录: {find_plain_dir()}")
        print(f"原库目录:   {source_dir()}（存在: {source_dir().is_dir()}）")
        return 0

    report = clean(
        plain_dir=pathlib.Path(args.dir) if args.dir else None,
        src_dir=pathlib.Path(args.src) if args.src else None,
        older_than_s=args.older_than,
        apply=args.apply,
    )

    if report["skipped_reason"]:
        print(f"✗ {report['skipped_reason']}")
        return 1

    mb = report["freed_bytes"] / 1024 / 1024
    verb = "已删" if report["applied"] else "将删"
    print(f"{verb} {len(report['files'])} 个文件，共 {mb:.1f} MB（目录 {report['dir']}）")
    for name in report["files"][:10]:
        print(f"  {name}")
    if len(report["files"]) > 10:
        print(f"  …还有 {len(report['files']) - 10} 个")
    if not args.apply and report["files"]:
        print("\n（预演；加 --apply 才删）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
