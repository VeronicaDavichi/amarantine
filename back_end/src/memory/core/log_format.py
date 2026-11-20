# -*- coding: utf-8 -*-
"""G3：日志格式可切 JSON（LOG_FORMAT=text|json）。

为什么需要：`admin.log` 现在是给人看的文本，采集/检索要正则。
JSON 行可以让 `jq` 直接取字段，也能被日志系统无痛接走。

**默认仍然是 text** —— 不改变现有行为，只有显式 `LOG_FORMAT=json` 才切换。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

#: 这些字段是 LogRecord 自带的，不当成「业务字段」输出
_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class JsonLogFormatter(logging.Formatter):
    """一行一个 JSON 对象。

    字段：`ts`（UTC ISO）、`level`、`logger`、`msg`，外加记录进 records 的业务字段
    （用 `logger.info("...", extra={"event_id": "..."})` 传的都会带上）。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(val)
            except (TypeError, ValueError):
                val = repr(val)
            payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
