"""日志：写文件，完全不使用 stdout / stderr。

原因：MCP 的 stdout 必须只承载 JSON-RPC 消息（写一个字节的日志都会被 host 解析失败），
stderr 在部分 host 里会被合并进协议流或被静默丢弃。落盘是唯一可靠通道。

所以**进程级致命错误也不写 stderr**——host 能感知的信号只有退出码，
失败原因去 `LOG_FILE` 里看。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from memory.config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s | %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
DEFAULT_LOG_FILE = "logs/memory.log"


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
    """记录致命错误——只写文件，不碰 std。

    配置阶段失败（settings 为 None）时日志系统尚未就绪，直接追加到
    `LOG_FILE`（环境变量优先，否则默认路径），不经过 logging，避免
    lastResort handler 把内容漏到 stderr。
    """
    if settings is None:
        target = Path(os.environ.get("LOG_FILE", DEFAULT_LOG_FILE))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(f"FATAL: {message}\n")
        except OSError:
            pass
        return

    logging.getLogger("memory").error("FATAL: %s", message)
