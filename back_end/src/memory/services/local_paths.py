# -*- coding: utf-8 -*-
"""本机路径解析（QQ 语料相关的外部工具路径）。

## 为什么放在 `src/` 而不是 harness

配置项必须有**真实消费点**（`tests/test_config_usage.py` 会查）。
`QQ_NT_MSG_DB` / `QQ_MCP_PLAIN_DIR` 是给导出、清理脚本用的，
但解析逻辑放这里，harness 只调用 —— 这样配置项真的有消费点，
而不是靠白名单把测试哄过去。

## 为什么需要「自动探测」

qq-mcp 是 **Windows 侧** node 进程，它的 `~` 是 `C:\\Users\\<用户>`，
不是 WSL 的 `/home/<用户>`。所以解密副本常出现在
`/mnt/c/Users/<用户>/.qq-mcp/plain`，而 `Path.home()` 指不到那里。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: 没配 `QQ_MCP_PLAIN_DIR` 时的兜底（Windows 用户目录经 WSL 挂载）
_WSL_USER_GLOB = "/mnt/c/Users/*/.qq-mcp/plain"

#: 允许清理的后缀：`.plain.db` 主副本 + 它的 wal/shm 边角文件
PLAIN_SUFFIXES = (".plain.db", ".plain.db-wal", ".plain.db-shm")


#: `back_end/` 目录（local_paths.py → services → memory → src → back_end）
BACK_END_DIR = Path(__file__).resolve().parents[3]


def default_env_file() -> Path:
    """项目约定的 `.env` 位置（不看 CWD）。"""
    return BACK_END_DIR / ".env"


def load_settings_from_project() -> Any:
    """加载配置，**显式指定** `back_end/.env`。

    `Settings.env_file=".env"` 相对 CWD 解析：从仓库根跑 harness 脚本会
    读不到配置（不报错，只是全用默认值——这种静默降级最难查）。
    """
    from memory.config import Settings

    env = default_env_file()
    return Settings(_env_file=str(env) if env.is_file() else None)


def _cfg_value(settings: Any, name: str) -> str:
    """取配置值；settings 为 None 时退回环境变量。"""
    val = getattr(settings, name, "") if settings is not None else ""
    return str(val or "").strip() or os.environ.get(name, "").strip()


def nt_msg_db_path(settings: Any = None) -> Path | None:
    """NT QQ 的消息库路径；没配返回 None（调用方决定是报错还是跳过）。"""
    raw = _cfg_value(settings, "QQ_NT_MSG_DB")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p


def nt_db_dir(settings: Any = None) -> Path | None:
    """原库目录（`nt_msg.db` 的父目录）。"""
    p = nt_msg_db_path(settings)
    return p.parent if p else None


def qq_plain_dir(settings: Any = None) -> Path:
    """qq-mcp 解密副本目录。

    解析顺序：
    1. 配置 `QQ_MCP_PLAIN_DIR`
    2. `~/.qq-mcp/plain`（原生 Linux/macOS）
    3. `/mnt/c/Users/*/.qq-mcp/plain`（WSL 里跑、qq-mcp 在 Windows）

    Returns:
        目录路径（**不保证存在**，调用方自己判断）。
    """
    raw = _cfg_value(settings, "QQ_MCP_PLAIN_DIR")
    if raw:
        return Path(raw).expanduser()
    local = Path.home() / ".qq-mcp" / "plain"
    if local.is_dir():
        return local
    for cand in Path("/mnt/c/Users").glob("*/.qq-mcp/plain"):
        if cand.is_dir():
            return cand
    return local

def mcp_pkg_dir(settings: Any = None) -> str:
    """MCP 实现包目录（`QQ_MCP_PKG`）。

    给的是「包目录」，脚本据此拼 `dist/index.js`。换实现（qq-mcp → im-mcp）
    只改配置，不改代码。

    Note:
        这里**不做路径转换**（Windows 盘符 ↔ WSL 挂载）—— 那取决于调用方
        是哪个平台的 node，由 `harness/load_env.mjs` 的 `resolvePath` 负责。
    """
    return _cfg_value(settings, "QQ_MCP_PKG")


def mcp_passphrase_file(settings: Any = None) -> str:
    """口令文件路径（`QQ_MCP_PASSPHRASE_FILE`）。"""
    return _cfg_value(settings, "QQ_MCP_PASSPHRASE_FILE")

def mcp_passphrase(settings: Any = None) -> str:
    """MCP 口令：优先 `QQ_MCP_PASSPHRASE`，否则读 `QQ_MCP_PASSPHRASE_FILE`。

    返回空串表示没配（调用方决定报错还是让 MCP 自己去找 `~/.qq-mcp/qqkey`）。
    """
    inline = _cfg_value(settings, "QQ_MCP_PASSPHRASE")
    if inline:
        return inline
    path = mcp_passphrase_file(settings)
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
