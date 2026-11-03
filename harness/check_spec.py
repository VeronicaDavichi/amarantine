#!/usr/bin/env python3
"""spec 骨架校验（补 `leanspec validate` 的空缺）。

`leanspec validate` 在本机报 `not yet migrated to the adapter API`，
MCP 的 `validate_spec` 又对 enum 字段报类型错 —— 两条官方校验路径都不可用。
但骨架约定是明确的：`.lean-spec/templates/spec-template.md` 定了五段，
`.lean-spec/config.json` 定了行数阈值。这个脚本把这几条钉死。

只查四项，够用且不过度约束：

  1. 每个 `specs/NNN-*/README.md` 有 frontmatter，且含 status/created/priority
  2. status 是合法值
  3. 正文含五个二级标题：Overview / Design / Plan / Test / Notes
  4. 行数 <= config.json 的 validation.maxLines

用法（仓库根目录）：
    ./harness/dev-linux.sh python ../harness/check_spec.py

退出码 0 = 全部合规，1 = 有漂移。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
CONFIG = ROOT / ".lean-spec" / "config.json"

#: `.lean-spec` 约定的五个二级标题。缺任何一个都说明骨架不是模板形状。
REQUIRED_SECTIONS = ("Overview", "Design", "Plan", "Test", "Notes")

#: frontmatter 必须有的三个字段。`created` 没有的话 spec 无法排序/统计。
REQUIRED_FIELDS = ("status", "created", "priority")

VALID_STATUS = {"draft", "planned", "in-progress", "complete", "archived"}


def load_max_lines() -> int:
    """从 config.json 读行数上限；文件缺失或字段缺失时退回 400。"""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(data.get("validation", {}).get("maxLines", 400))
    except (OSError, ValueError, TypeError):
        return 400


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 frontmatter 的顶层标量字段和正文。

    只认顶层 `key: value`，不解析 YAML 列表 —— 这里只查字段在不在、
    status 是不是合法值，不需要完整 YAML。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("'\"")
    return meta, parts[2]


def check_one(path: Path, max_lines: int) -> list[str]:
    """检查一个 spec，返回问题列表（空 = 合规）。"""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"读不到: {exc}"]

    meta, body = split_frontmatter(text)
    if not meta:
        problems.append("缺 frontmatter（文件应以 --- 开头）")
    else:
        for field in REQUIRED_FIELDS:
            if not meta.get(field):
                problems.append(f"frontmatter 缺字段 `{field}`")
        status = meta.get("status", "")
        if status and status not in VALID_STATUS:
            problems.append(
                f"status `{status}` 不合法（应为 {'/'.join(sorted(VALID_STATUS))}）"
            )

    headings = {
        m.group(1).strip()
        for m in re.finditer(r"^##\s+(.+)$", body, re.MULTILINE)
    }
    for section in REQUIRED_SECTIONS:
        if section not in headings:
            problems.append(f"缺章节 `## {section}`")

    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if lines > max_lines:
        problems.append(f"{lines} 行，超过上限 {max_lines}")

    return problems


def main() -> int:
    if not SPECS.is_dir():
        print(f"FAIL: 找不到 specs 目录 {SPECS}")
        return 1

    max_lines = load_max_lines()
    spec_dirs = sorted(p for p in SPECS.iterdir() if p.is_dir())
    if not spec_dirs:
        print("FAIL: specs 下没有任何 spec")
        return 1

    failures = 0
    for spec_dir in spec_dirs:
        readme = spec_dir / "README.md"
        if not readme.is_file():
            print(f"  [FAIL] {spec_dir.name} — 缺 README.md")
            failures += 1
            continue
        problems = check_one(readme, max_lines)
        if problems:
            failures += 1
            print(f"  [FAIL] {spec_dir.name}")
            for p in problems:
                print(f"         - {p}")
        else:
            print(f"  [PASS] {spec_dir.name}")

    print()
    if failures:
        print(f"FAIL: {failures}/{len(spec_dirs)} 个 spec 不合骨架")
        return 1
    print(f"OK: spec 骨架一致（{len(spec_dirs)} 个，行数上限 {max_lines}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
