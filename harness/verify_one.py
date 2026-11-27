#!/usr/bin/env python3
"""按单个 spec 跑验证点 + harness + e2e —— /leanspec-complete 的机械部分。

它只做三件可判定的事，不做「看起来应该没问题」：

1. **验证点**：收集 spec 目录下所有 markdown 的 `- [ ]` / `- [x]`（跳过代码块内的字面量），
   列出未勾项 —— 这正是 `leanspec update --status complete` 的硬门禁。
2. **可执行验证命令**：spec 里 fenced code block 内以 `$ ` 开头的行。
   这是本项目唯一认的「可跑验证点」约定；没有 `$ ` 行就是「只能人工验证」。
3. **harness / e2e / visual**：存在就跑，未集成（exit 2）按能力缺失记录。

用法（仓库根目录）：
    python3 harness/verify_one.py 003-my-spec            # 实跑
    python3 harness/verify_one.py 003-my-spec --dry-run  # 只列计划
    python3 harness/verify_one.py 003-my-spec --fail-fast

退出码：
    0 = 全部可执行项绿（且没有「UI 需求但 E2E 未集成」这类阻塞）
    1 = 有失败项
    2 = 没有可自动验证项（只能人工），或 spec 找不到/有歧义
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
ROOT = HARNESS.parent
SPECS = ROOT / "specs"

SPEC_FILES = ("README.md", "requirements.md", "design.md", "tasks.md")

#: 判定「这条 spec 有 UI / 原型需求」的关键词 —— 只用来**提醒人工复核**，
#: 不直接触发 e2e。真正触发看 spec 自己有没有声明（见 ui_declared）。
UI_KEYWORDS = (
    "页面", "前端", "表单", "按钮", "界面", "交互", "点击", "输入",
    "列表展示", "提示", "跳转", "原型", "截图", "Figma", ".meoo.zone",
)

DEFAULT_TIMEOUT = 600


def die(msg: str, code: int = 2) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return code


def resolve_spec(arg: str) -> Path | None:
    """按目录名 / 编号前缀 / 唯一子串解析 spec 目录。"""
    dirs = sorted(p for p in SPECS.iterdir() if p.is_dir()) if SPECS.is_dir() else []
    if not dirs:
        return None
    for d in dirs:
        if d.name == arg:
            return d
    by_prefix = [d for d in dirs if d.name.startswith(arg)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    by_sub = [d for d in dirs if arg.lower() in d.name.lower()]
    if len(by_sub) == 1:
        return by_sub[0]
    if len(by_sub) > 1:
        print(f"歧义：'{arg}' 匹配到多个 spec：", file=sys.stderr)
        for d in by_sub:
            print(f"  - {d.name}", file=sys.stderr)
    return None


def iter_lines(text: str):
    """逐行产出 (行号, 内容, 是否在 fenced code block 内)。"""
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            yield i, line, True
            continue
        yield i, line, in_fence


def collect_checkboxes(spec_dir: Path):
    """[(file, line_no, checked, text)]，跳过代码块。"""
    items = []
    for name in SPEC_FILES:
        path = spec_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line, in_fence in iter_lines(text):
            if in_fence:
                continue
            m = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", line)
            if m:
                items.append((name, line_no, m.group(1).lower() == "x", m.group(2).strip()))
    return items


def collect_commands(spec_dir: Path):
    """spec 里 `$ ` 开头的可执行验证命令（只认代码块内）。"""
    cmds = []
    seen = set()
    for name in SPEC_FILES:
        path = spec_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for _, line, in_fence in iter_lines(text):
            if not in_fence:
                continue
            m = re.match(r"^\s*\$\s+(.+?)\s*$", line)
            if not m:
                continue
            cmd = m.group(1)
            if cmd in seen:
                continue
            seen.add(cmd)
            cmds.append((name, cmd))
    return cmds


def spec_text(spec_dir: Path) -> str:
    parts = []
    for name in SPEC_FILES:
        path = spec_dir / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def slug_of(spec_dir: Path) -> str:
    """001-my-feature -> my-feature（e2e 的 grep 过滤词）。"""
    name = spec_dir.name
    m = re.match(r"^\d+[-_]?(.*)$", name)
    return (m.group(1) if m and m.group(1) else name).strip("-") or name


def run(cmd: str, timeout: int):
    """跑一条命令，返回 (exit_code, 输出尾部)。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）：{cmd}"
    except OSError as exc:
        return 127, f"无法执行：{exc}"


def tail(text: str, lines: int = 12) -> str:
    rows = [r for r in text.splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def main() -> int:
    ap = argparse.ArgumentParser(description="跑单个 spec 的验证点 / harness / e2e")
    ap.add_argument("spec", help="spec 目录名 / 编号 / 唯一子串，例如 003 或 003-my-feature")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    ap.add_argument("--fail-fast", action="store_true", help="首条失败即停")
    ap.add_argument("--no-harness", action="store_true", help="不跑 harness 脚本")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单条命令超时秒数")
    args = ap.parse_args()

    if not SPECS.is_dir():
        return die(f"找不到 specs 目录：{SPECS}（先跑 /leanspec-init）")

    spec_dir = resolve_spec(args.spec)
    if spec_dir is None:
        dirs = sorted(p.name for p in SPECS.iterdir() if p.is_dir())
        return die(f"没能唯一匹配到 spec '{args.spec}'。现有：{', '.join(dirs) or '(空)'}")

    boxes = collect_checkboxes(spec_dir)
    outstanding = [b for b in boxes if not b[2]]
    cmds = collect_commands(spec_dir)
    text = spec_text(spec_dir)
    # 是否跑 e2e / visual，由 spec **自己声明**决定（propose 会把 verify-e2e.sh 写进任务里）。
    # 关键词只用来提醒人工复核，避免「凡是提到前端就误判成 UI spec」。
    ui_declared = ("verify-e2e.sh" in text) or bool(re.search(r"\be2e/", text))
    ui_hints = sorted({k for k in UI_KEYWORDS if k in text})
    has_proto = (spec_dir / "assets" / "prototype").is_dir() or (spec_dir / "assets" / "routes.json").is_file()
    visual_declared = has_proto or ("verify-visual.sh" in text)
    slug = slug_of(spec_dir)
    rel = spec_dir.relative_to(ROOT).as_posix()

    print("=" * 64)
    print(f" spec: {spec_dir.name}")
    print(f" 形态: {'多文件' if (spec_dir / 'tasks.md').is_file() else '单文件'}"
          f" · E2E声明={'是' if ui_declared else '否'}"
          f" · 原型资产={'有' if has_proto else '无'}")
    print(f" 验证点: {len(boxes) - len(outstanding)}/{len(boxes)} 已勾"
          f" · 可执行命令: {len(cmds)} 条")
    print("=" * 64)

    if ui_hints and not ui_declared:
        print(f"\n⚠️ 文本含 UI 关键词（{', '.join(ui_hints[:6])}）但未声明 e2e —— "
              f"请人工确认：若确有 UI 需求，任务里必须补 verify-e2e.sh 与 e2e/<slug>.spec.ts")

    if outstanding:
        print(f"\n未勾的验证点（{len(outstanding)}）：")
        for name, line_no, _, item in outstanding:
            print(f"  [ ] {item}  ({name}:{line_no})")
    else:
        print("\n验证点：全部已勾")

    if not cmds:
        print("\n可执行命令：无（本 spec 没有 `$ ` 验证命令）")

    stats = {"pass": 0, "fail": 0, "skip": 0}
    failures = []
    blockers = []

    # ---- 1. spec 自带的验证命令
    for source, cmd in cmds:
        if args.dry_run:
            print(f"\n[DRY] $ {cmd}    ({source})")
            continue
        print(f"\n$ {cmd}    ({source})")
        code, out = run(cmd, args.timeout)
        if code == 0:
            stats["pass"] += 1
            last = tail(out, 2)
            if last:
                print("  ✓ exit 0")
                for row in last.splitlines():
                    print(f"    {row}")
        else:
            stats["fail"] += 1
            failures.append((cmd, code, out))
            print(f"  ✗ exit {code}")
            for row in tail(out, 12).splitlines():
                print(f"    {row}")
            if args.fail_fast:
                break

    # ---- 2. harness 套件
    if not args.no_harness:
        plan = []
        if (HARNESS / "verify-spec.sh").is_file():
            plan.append(("verify-spec.sh", "./harness/verify-spec.sh", None))
        if (HARNESS / "verify-e2e.sh").is_file() and ui_declared:
            plan.append(("verify-e2e.sh", f"./harness/verify-e2e.sh {slug} headless", "e2e"))
        if (HARNESS / "verify-visual.sh").is_file() and visual_declared:
            plan.append(("verify-visual.sh", f"./harness/verify-visual.sh {rel}", "visual"))

        for label, cmd, kind in plan:
            if args.dry_run:
                print(f"\n[DRY] $ {cmd}    (harness/{label})")
                continue
            print(f"\n$ {cmd}    (harness/{label})")
            code, out = run(cmd, args.timeout)
            if code == 0:
                stats["pass"] += 1
                print("  ✓ exit 0")
                for row in tail(out, 3).splitlines():
                    print(f"    {row}")
            elif code == 2:
                stats["skip"] += 1
                print("  ⊘ exit 2（capability 未集成）")
                for row in tail(out, 4).splitlines():
                    print(f"    {row}")
                if kind in ("e2e", "visual"):
                    what = "E2E" if kind == "e2e" else "视觉对照"
                    blockers.append(f"{label} 未集成，但本 spec 声明了 {what} 验证")
            else:
                stats["fail"] += 1
                failures.append((cmd, code, out))
                print(f"  ✗ exit {code}")
                for row in tail(out, 12).splitlines():
                    print(f"    {row}")
                if args.fail_fast:
                    break

    # ---- 3. 结论
    print("\n" + "=" * 64)
    print(f" 汇总: 绿 {stats['pass']} · 红 {stats['fail']} · 跳过 {stats['skip']}")
    if args.dry_run:
        print(" DRY-RUN：未执行任何命令")
        print("=" * 64)
        return 0

    if failures:
        print("\n失败项：")
        for cmd, code, _ in failures:
            print(f"  ✗ [{code}] {cmd}")
    if blockers:
        print("\n阻塞项（不能算完成）：")
        for b in blockers:
            print(f"  ⊘ {b}")
    if outstanding:
        print(f"\n未勾验证点 {len(outstanding)} 项 —— `leanspec update --status complete` 会拒绝，"
              f"不要用 --force 绕过。")

    print("=" * 64)

    if stats["fail"] or blockers:
        return 1
    if stats["pass"] == 0:
        print("\n结论：没有可自动验证项 —— 只能人工验证，请在 spec 的 Notes 里记录人工结论。")
        return 2
    if outstanding:
        print("\n结论：命令全绿，但仍有未勾验证点 —— 勾选后方可标记 complete。")
        return 1
    print("\n结论：全绿，可以执行 leanspec update <spec> --status complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
