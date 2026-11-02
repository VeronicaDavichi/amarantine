#!/usr/bin/env python3
"""文档一致性检查（E4）。

文档最大的问题是会**悄悄过期**：代码加了配置项，文档没写；
工具改了名，文档还留着旧的。这个脚本把它们对齐。

检查三类：
1. 文档里提到的 `ENV_VAR` 必须在 `config.py` 里真实存在
2. `config.py` 里的配置项必须在文档里有记录（关键项）
3. 文档里提到的工具名必须和 MCP 注册的一致

用法（仓库根目录）：
    ./harness/dev-linux.sh python ../harness/check_guide.py

退出码 0 = 一致，1 = 有漂移。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = ROOT / "back_end" / "src" / "memory" / "config.py"
MCP_PY = ROOT / "back_end" / "src" / "memory" / "entrypoints" / "mcp_stdio.py"
DOCS = [
    ROOT / "docs" / "memory-tool-guide.md",
    ROOT / "docs" / "user-manual.md",
]

#: 这些配置项属于内部调参，不要求出现在用户文档里
INTERNAL_PREFIXES = (
    "DB_",
    "SQL_",
    "ALEMBIC_",
    "PYDANTIC_",
    "MODEL_",
    "LOG_",
    "TZ",
)


def load_settings_names() -> set[str]:
    """从 config.py 抽出所有大写配置字段名。"""
    text = CONFIG_PY.read_text(encoding="utf-8")
    return set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]{2,})\s*:", text, re.MULTILINE))


def load_tool_names() -> set[str]:
    """从 mcp_stdio.py 抽出注册的工具名。

    工具注册成 `TOOL_HANDLERS = {"memory_recall": _tool_recall, ...}`，
    所以模式是 `"memory_xxx":`。
    """
    text = MCP_PY.read_text(encoding="utf-8")
    return set(re.findall(r'"(memory_[a-z_]+)"\s*:', text))


def doc_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in DOCS if p.exists())


def env_vars_in_doc(text: str) -> set[str]:
    """文档里写成 `ALL_CAPS` 或 ALL_CAPS 形式的环境变量。"""
    # 只取看起来像配置项的：2 段以上下划线，或者已知后缀
    candidates = set(re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text))
    # 排除明显不是配置的词（如 HTTP 动词、错误前缀）
    noise = {
        "MEMORY_TOOL", "SESSION_ID", "TOOL_NAME",
    }
    return candidates - noise


def is_internal(name: str) -> bool:
    return any(name.startswith(p) for p in INTERNAL_PREFIXES)


def main() -> int:
    if not CONFIG_PY.exists():
        print(f"FATAL: 找不到 {CONFIG_PY}")
        return 1

    settings = load_settings_names()
    tools = load_tool_names()
    text = doc_text()
    doc_vars = env_vars_in_doc(text)

    problems: list[str] = []

    # 1) 文档提到但代码里不存在的配置项
    unknown = sorted(v for v in doc_vars - settings if not is_internal(v))
    # 允许文档里出现的非配置大写词（错误名/常量名）
    allowed_extra = {
        "RECALL_UNAVAILABLE", "LABEL_REVIEW", "NEED_REVIEW",
        "SAFE_TO_DELETE", "PROTECTED", "USER_FORGET", "OUTDATED",
        "NO_CANDIDATE", "BELOW_THRESHOLD", "CLAMP_MAX", "CLAMP_MIN",
        "DROP_RATIO", "OVERFLOW_FORCED", "HTTP", "ADR", "MCP", "LLM",
        "FAQ", "TTL", "JSON", "API", "URL", "SQL", "RRF", "BM25",
        "UTF", "CPU", "RAM", "SQLITE", "PG",
        # 非配置项：代码常量 / 外部工具的环境变量 / shell 层开关
        "SCHEMA_PROPERTIES", "UV_PROJECT_ENVIRONMENT", "LOG_FILE",
        "PYTHONIOENCODING", "MEMORY_KEEP_LLM_BASE_URL",
        # 非配置项：Neo4j 关系类型名 / 节点标签 / 图上的字符串枚举
        "RELATES_TO", "MENTIONS", "RELATED", "INVOLVES", "ACTION",
        "ALIAS_OF", "Alias", "Person", "Event", "Action",
        # 非配置项：模型层常量
        "ALIAS_PROBABLE_MIN", "ALIAS_CONFIDENCE", "ALIAS_EXACT",
        "ALIAS_PROBABLE", "ALIAS_UNCERTAIN",
        # 非配置项：cypher 常量名（文档里讲图的实现时会提到）
        "APPEND_ALIAS_CYPHER", "RESOLVE_ALIAS_CYPHER",
        "APPEND_PERSON_CYPHER", "APPEND_ENTITY_CYPHER",
        "LIST_KNOWN_NAMES_CYPHER",
        # 非配置项：labeler 里的分词常量（文档里讲称谓词典时会提到）
        "RANK_DIGITS", "RANK_SUFFIXES",
    }
    unknown = [v for v in unknown if v not in allowed_extra]
    for v in unknown:
        problems.append(f"文档提到 `{v}`，但 config.py 里没有这个配置项")

    # 2) 关键配置项文档必须覆盖
    must_document = {
        "MEMORY_NAMESPACE",
        "ADMIN_TOKEN",
        "RECALL_TOP_K_MIN",
        "RECALL_TOP_K_MAX",
        "RECALL_COUNT_MIN_SCORE",
        "RECALL_DROP_RATIO",
        "RECALL_GAP_RATIO",
        "CONTEXT_TOKEN_BUDGET",
        "RECALL_BUDGET_MS",
        "FORGET_IDLE_DAYS",
        "FORGET_MIN_RECALLS",
        "FORGET_GRACE_DAYS",
        "PROTECT_IMPORTANCE_REVIEW",
        "DATABASE_URL",
    }
    for name in sorted(must_document & settings):
        if name not in text:
            problems.append(f"关键配置 `{name}` 没有写进文档")

    # 3) 工具名对齐
    doc_tools = set(re.findall(r"\b(memory_[a-z_]+)\b", text))
    for name in sorted(doc_tools - tools):
        problems.append(f"文档提到工具 `{name}`，但 MCP 没有注册")
    for name in sorted(tools - doc_tools):
        problems.append(f"MCP 注册了 `{name}`，但文档没提到")

    # 4) 工具状态表里的「待实现」要和代码一致
    mcp_text = MCP_PY.read_text(encoding="utf-8")
    handlers = dict(
        re.findall(r'"(memory_[a-z_]+)"\s*:\s*(_tool_[a-z_]+)', mcp_text)
    )
    for name in sorted(tools):
        handler = handlers.get(name)
        not_impl = False
        if handler:
            # 切出 handler 函数体，看里面有没有 raise NotImplementedError
            m = re.search(
                rf"def {re.escape(handler)}\(.*?(?=\ndef |\nTOOL_HANDLERS|\Z)",
                mcp_text,
                re.DOTALL,
            )
            if m:
                not_impl = "NotImplementedError" in m.group(0)
        doc_says_pending = bool(
            re.search(rf"`{name}`[^\n]*待实现", text)
        )
        if not_impl and not doc_says_pending:
            problems.append(f"`{name}` 在代码里还没实现，但文档没说")
        if not not_impl and doc_says_pending:
            problems.append(f"`{name}` 代码已实现，但文档还写着待实现")

    # 输出
    print(f"配置项 {len(settings)} 个 | 工具 {len(tools)} 个 | 文档变量 {len(doc_vars)} 个")
    print(f"工具: {', '.join(sorted(tools))}")
    if problems:
        print(f"\n发现 {len(problems)} 处漂移：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nOK: 文档与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
