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
    # 格式可切：默认 text；LOG_FORMAT=json 时一行一对象（便于 jq/采集）
    if str(getattr(settings, "LOG_FORMAT", "text")).lower() == "json":
        from memory.core.log_format import JsonLogFormatter

        file_handler.setFormatter(JsonLogFormatter())
    else:
        file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    # 明确不挂 StreamHandler：std 通道留给协议
    return logging.getLogger("memory")


def fatal(settings: Settings | None, message: str) -> None:
    """记录致命错误。

    ## 为什么这里**允许**写 stderr

    模块开头说「进程级致命错误也不写 stderr」，那是针对**运行期**的：
    协议建立后任何非协议字节都会让宿主解析失败。

    但**启动阶段**（走到这里就 return 1 退出）协议还没建立，stdout 空着，
    此时唯一的可观测信号是退出码 —— 而宿主把「进程起来又退出」显示成
    「已连接但没有工具」，真正的原因（`MEMORY_NAMESPACE` 非法之类）
    只躺在 `/tmp/xxx.log` 里没人会去看。

    实测踩过：宿主配置里 `bash -lc` 没加引号 → namespace 没注入 →
    配置校验失败 → 退出码 1 + 日志有原因 → 宿主只显示「未注册任何工具」，
    排查花了一整轮。

    所以：**启动期致命错误写 stderr**（帮宿主显示原因），日志照旧写文件。
    """
    if settings is None:
        target = Path(os.environ.get("LOG_FILE", DEFAULT_LOG_FILE))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(f"FATAL: {message}\n")
        except OSError:
            pass
        _echo_fatal(message)
        return

    logging.getLogger("memory").error("FATAL: %s", message)
    _echo_fatal(message)


def _echo_fatal(message: str) -> None:
    """把启动期致命错误也写到 stderr（宿主通常能显示它）。"""
    try:
        import sys

        print(f"amarantine mcp FATAL: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - 连 stderr 都没有就算了
        pass
