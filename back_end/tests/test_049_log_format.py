"""spec 049 G3：日志格式可切 JSON（LOG_FORMAT=json）。"""

from __future__ import annotations

import json
import logging

from memory.core.log_format import JsonLogFormatter


def _record(msg: str, *, level: int = logging.INFO, **extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="memory.test", level=level, pathname=__file__, lineno=10,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_one_line_object() -> None:
    out = JsonLogFormatter().format(_record("测试消息"))
    assert "\n" not in out
    data = json.loads(out)
    assert data["msg"] == "测试消息"
    assert data["level"] == "INFO"
    assert data["logger"] == "memory.test"
    assert data["ts"].endswith("+00:00")


def test_extra_fields_are_included() -> None:
    """业务字段（event_id 等）要能直接用 jq 取。"""
    out = JsonLogFormatter().format(_record("写入", event_id="evt_abc", count=3))
    data = json.loads(out)
    assert data["event_id"] == "evt_abc"
    assert data["count"] == 3


def test_reserved_fields_not_leaked_as_business() -> None:
    data = json.loads(JsonLogFormatter().format(_record("x")))
    for key in ("filename", "lineno", "process", "thread", "pathname"):
        assert key not in data, key


def test_non_serializable_extra_becomes_repr() -> None:
    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"

    data = json.loads(JsonLogFormatter().format(_record("x", obj=_Weird())))
    assert data["obj"] == "<weird>"


def test_exception_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record("失败", level=logging.ERROR)
        rec.exc_info = sys.exc_info()
    data = json.loads(JsonLogFormatter().format(rec))
    assert "ValueError: boom" in data["exc"]


def test_ensure_ascii_false_keeps_chinese() -> None:
    out = JsonLogFormatter().format(_record("中文不转义"))
    assert "中文不转义" in out, "中文不该变成 \\uXXXX"


def test_text_format_still_default(monkeypatch, tmp_path) -> None:
    """默认（不设 LOG_FORMAT）仍然是可读文本，不改变现有行为。"""
    from memory.core.logging import setup_logging

    class _S:
        LOG_FILE = str(tmp_path / "m.log")
        LOG_LEVEL = "INFO"
        LOG_FORMAT = "text"

    setup_logging(_S())
    logging.getLogger("memory.test").info("hello-text")
    text = (tmp_path / "m.log").read_text(encoding="utf-8")
    assert "hello-text" in text
    assert not text.strip().startswith("{")
