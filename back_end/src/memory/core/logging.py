"""日志：写文件，不走 stdout / stderr。

原因：MCP 的 stdout 必须只承载 JSON-RPC 消息（写一个字节的日志都会被 host 解析失败），
而 stderr 在部分 host 里会被合并进协议流或被静默丢弃。落盘是唯一可靠通道。

唯一例外：进程级致命错误（配置加载失败、namespace 缺失）会在 stderr 留一行，
因为此时日志系统可能尚未就绪，且「进程没起来」需要让 host/用户立刻感知。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from memory.config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s | %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(settings: Settings) -> logging.Logger:
    """初始化文件日志；重复调用不会产生重复 handler。"""
    log_path = Path(settings.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    # 明确不挂 StreamHandler：std 通道留给协议
    return logging.getLogger("memory")


def fatal(settings: Settings | None, message: str) -> None:
    """记录致命错误并保留一行 stderr。"""
    logging.getLogger("memory").error("FATAL: %s", message)
    if settings is None:
        # 配置阶段失败：日志文件可能还没建，兜底写一次
        try:
            path = Path("logs/memory.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"FATAL: {message}\n")
        except OSError:
            pass
    print(f"FATAL: {message}", file=sys.stderr, flush=True)
