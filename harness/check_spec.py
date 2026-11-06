#!/usr/bin/env python3
"""spec 骨架校验（补 `leanspec validate` 的空缺）。

`leanspec validate` 在本机报 `not yet migrated to the adapter API`，
MCP 的 `validate_spec` 又对 enum 字段报类型错 —— 两条官方校验路径都不可用。
但骨架约定是明确的：`.lean-spec/templates/spec-template.md` 定了五段，
`.lean-spec/config.json` 定了行数阈值。这个脚本把这几条钉死。

**两种形态都认**（2026-11-06 起，036/037 起改用多文件）：

| 形态 | 判定 | 要求 |
|---|---|---|
| 单文件 | 只有 `README.md` | README 有 frontmatter + 五个二级标题（Overview/Design/Plan/Test/Notes） |
| 多文件 | `requirements.md` + `design.md` + `tasks.md` 三份齐全 | README 是薄入口（frontmatter + ≥1 个二级标题 + 引用三份文件），三份各自非空且有二级标题 |

两种形态都要求：frontmatter 含 status/created/priority、status 合法、每个文件
行数 ≤ `config.json` 的 `validation.maxLines`。

**部分迁移要报错**：三份里只有一两份 → FAIL，避免「一半单文件一半多文件」的
暧昧状态（那种 spec 谁都读不懂）。

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

#: 单文件形态要求的五个二级标题。
REQUIRED_SECTIONS = ("Overview", "Design", "Plan", "Test", "Notes")

#: 多文件形态的三件套。齐全 = 多文件，缺一份 = 迁移未完成。
MULTI_FILES = ("requirements.md", "design.md", "tasks.md")

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


def count_lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def check_frontmatter(meta: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if not meta:
        return ["缺 frontmatter（文件应以 --- 开头）"]
    for field in REQUIRED_FIELDS:
        if not meta.get(field):
            problems.append(f"frontmatter 缺字段 `{field}`")
    status = meta.get("status", "")
    if status and status not in VALID_STATUS:
        problems.append(f"status `{status}` 不合法（应为 {'/'.join(sorted(VALID_STATUS))}）")
    return problems


def has_section(body: str) -> bool:
    return bool(re.search(r"^##\s+\S", body, re.MULTILINE))


def _link_re(name: str) -> re.Pattern[str]:
    """README 里指向三件套的**真实** markdown 链接。

    只查文件名字符串会出现假绿：`[tasks.md](taskz.md)` 里名字在、链接是坏的。
    所以要求 `](name)` 形态，允许 `./` 前缀。
    """
    return re.compile(r"\]\(\s*(?:\./)?" + re.escape(name) + r"\s*\)")


def check_single(text: str, max_lines: int) -> list[str]:
    """单文件形态：frontmatter + 五段 + 行数。"""
    meta, body = split_frontmatter(text)
    problems = check_frontmatter(meta)

    headings = {m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", body, re.MULTILINE)}
    for section in REQUIRED_SECTIONS:
        if section not in headings:
            problems.append(f"缺章节 `## {section}`")

    lines = count_lines(text)
    if lines > max_lines:
        problems.append(f"{lines} 行，超过上限 {max_lines}")
    return problems


def check_multi(spec_dir: Path, readme_text: str, max_lines: int) -> list[str]:
    """多文件形态：README 薄入口 + 三件套齐全 + 各自有二级标题。"""
    meta, body = split_frontmatter(readme_text)
    problems = check_frontmatter(meta)

    if not has_section(body):
        problems.append("README 至少要有 1 个二级标题（如 `## 目标`）")
    for name in MULTI_FILES:
        if not _link_re(name).search(readme_text):
            problems.append(f"README 未链接 `{name}`（薄入口必须给出可点的导航链接）")

    lines = count_lines(readme_text)
    if lines > max_lines:
        problems.append(f"README {lines} 行，超过上限 {max_lines}")

    for name in MULTI_FILES:
        path = spec_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{name} 读不到: {exc}")
            continue
        if not text.strip():
            problems.append(f"{name} 是空文件")
            continue
        if not has_section(text):
            problems.append(f"{name} 至少要有 1 个二级标题")
        n = count_lines(text)
        if n > max_lines:
            problems.append(f"{name} {n} 行，超过上限 {max_lines}")
    return problems


def check_one(spec_dir: Path, max_lines: int) -> tuple[list[str], str]:
    """检查一个 spec，返回 (问题列表, 形态)。形态用于输出里区分。"""
    readme = spec_dir / "README.md"
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"读不到 README.md: {exc}"], "?"

    present = [name for name in MULTI_FILES if (spec_dir / name).is_file()]
    if present and len(present) != len(MULTI_FILES):
        missing = [n for n in MULTI_FILES if n not in present]
        return (
            [f"多文件迁移不完整：缺 {', '.join(missing)}（三份要么齐全，要么都别建）"],
            "partial",
        )
    if present:
        return check_multi(spec_dir, text, max_lines), "multi"
    return check_single(text, max_lines), "single"


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
    shape_count = {"single": 0, "multi": 0, "partial": 0, "?": 0}
    for spec_dir in spec_dirs:
        problems, shape = check_one(spec_dir, max_lines)
        shape_count[shape] = shape_count.get(shape, 0) + 1
        tag = "(multi)" if shape == "multi" else ""
        if problems:
            failures += 1
            print(f"  [FAIL] {spec_dir.name} {tag}".rstrip())
            for p in problems:
                print(f"         - {p}")
        else:
            print(f"  [PASS] {spec_dir.name} {tag}".rstrip())

    print()
    if failures:
        print(f"FAIL: {failures}/{len(spec_dirs)} 个 spec 不合骨架")
        return 1
    print(
        f"OK: spec 骨架一致（{len(spec_dirs)} 个："
        f"单文件 {shape_count['single']} / 多文件 {shape_count['multi']}，"
        f"行数上限 {max_lines}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
