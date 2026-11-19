#!/usr/bin/env python3
"""从 config.py 生成配置清单（spec 049 G3）。

只做机械提取：字段名、默认值、紧邻的注释。**不编造用途说明** ——
注释里没写的，就在表格里留空，让人知道「这项其实没有文档」。
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = ROOT / "back_end" / "src" / "memory" / "config.py"
OUT = ROOT / "docs" / "config-reference.md"

#: 按前缀分组（顺序即文档顺序）
GROUPS = [
    ("LLM", "LLM 网关与超时"),
    ("EMBEDDING", "向量模型"),
    ("RERANK", "重排"),
    ("WEAVIATE", "向量库"),
    ("NEO4J", "图库"),
    ("RECALL", "召回"),
    ("ABBR", "缩写与别名扩展"),
    ("TIME", "时间解析"),
    ("ENTITY", "实体与消歧"),
    ("ALIAS", "别名"),
    ("ADMIN", "管理接口"),
    ("MCP", "MCP 入口"),
    ("DB", "SQLite"),
    ("MEMORY", "记忆域与保留"),
    ("MAINTENANCE", "后台维护"),
    ("REVIEW", "复盘与反馈"),
    ("RETENTION", "遗忘"),
    ("GATE", "闸门"),
    ("CHUNK", "分块"),
    ("CONTEXT", "上下文"),
    ("", "其他"),
]


def main() -> int:
    src = CFG.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    fields: list[tuple[str, str, str]] = []  # (name, default, comment)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any("Settings" in b.id for b in node.bases if isinstance(b, ast.Name)):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            name = getattr(stmt.target, "id", None)
            if not name or not name.isupper():
                continue
            default = ""
            if stmt.value is not None:
                try:
                    default = ast.unparse(stmt.value)
                except Exception:  # noqa: BLE001
                    default = "?"
            # 紧邻的上方注释
            comment = ""
            ln = stmt.lineno - 2
            while ln >= 0 and lines[ln].strip().startswith("#"):
                comment = lines[ln].strip().lstrip("# ").strip() + (
                    ("；" + comment) if comment else ""
                )
                ln -= 1
            fields.append((name, default, comment))

    fields.sort(key=lambda x: x[0])
    buckets: dict[str, list[tuple[str, str, str]]] = {g: [] for g, _ in GROUPS}
    for name, default, comment in fields:
        for prefix, _title in GROUPS:
            if prefix and name.startswith(prefix):
                buckets[prefix].append((name, default, comment))
                break
        else:
            buckets[""].append((name, default, comment))

    out = [
        "# 配置项参考",
        "",
        f"由 `harness/gen_config_doc.py` 从 `back_end/config.py` 自动生成，共 **{len(fields)}** 项。",
        "",
        "> **注释为空的项表示源码里没写说明** —— 不是这里漏了。",
        "> 修改任何一项前先读对应 spec；标「不要改」的项改动会破坏数据一致性。",
        "",
    ]
    for prefix, title in GROUPS:
        items = buckets.get(prefix) or []
        if not items:
            continue
        out += [f"## {title}（{len(items)} 项）", "",
                "| 配置项 | 默认值 | 说明 |", "|---|---|---|"]
        for name, default, comment in items:
            d = default.replace("|", "\\|")
            c = (comment or "").replace("|", "\\|")
            out.append(f"| `{name}` | `{d}` | {c} |")
        out.append("")

    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"写出 {OUT}（{len(fields)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
