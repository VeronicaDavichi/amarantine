"""A5 MCP stdio 入口：JSON-RPC 2.0 over stdin/stdout。

硬约束：
  - **stdout 只写协议消息**，任何日志必须走 stderr，否则会把协议流污染成解析错误。
  - 一行一个 JSON 对象（MCP 用换行分隔的 JSON-RPC，不是 batch）。

启动：
    MEMORY_NAMESPACE=agent-a uv run memory-mcp
"""

from __future__ import annotations

import json
import sys
from typing import Any

from memory.config import Settings, load_settings
from memory.core.tools import INSTRUCTIONS, PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, TOOLS

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _result(msg_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _tool_text(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """把业务结果包成 MCP tool result。"""
    out: dict[str, Any] = {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ]
    }
    if is_error:
        out["isError"] = True
    return out


# ---------- 工具处理器（M1 为桩，B/C/E 阶段逐个实现） ----------


def _tool_recall(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("memory_recall 尚未实现（阶段 C7）")


def _tool_remember(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("memory_remember 尚未实现（阶段 B1）")


def _tool_forget(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("memory_forget 尚未实现（阶段 E1）")


def _tool_feedback(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("memory_feedback 尚未实现（阶段 E2）")


HANDLERS = {
    "memory_recall": _tool_recall,
    "memory_remember": _tool_remember,
    "memory_forget": _tool_forget,
    "memory_feedback": _tool_feedback,
}


# ---------- 方法分派 ----------


def handle_message(msg: dict[str, Any], settings: Settings) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        namespace = settings.MEMORY_NAMESPACE
        _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
                "_meta": {"namespace": namespace},
            },
        )
        _log(f"initialize: namespace={namespace!r}")

    elif method == "tools/list":
        _result(msg_id, {"tools": list(TOOLS.values())})

    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if name not in TOOLS:
            _error(msg_id, JSONRPC_INVALID_PARAMS, f"unknown tool: {name!r}")
            return

        # 入参不允许携带 namespace——它只能来自进程环境
        if "namespace" in args:
            _result(
                msg_id,
                _tool_text(
                    {"error": "namespace 不可作为入参传入；它由 MEMORY_NAMESPACE 环境变量决定"},
                    is_error=True,
                ),
            )
            return

        try:
            payload = HANDLERS[name](settings, args)
        except NotImplementedError as exc:
            _result(msg_id, _tool_text({"error": str(exc)}, is_error=True))
        except Exception as exc:  # 兜底：任何异常都转成 tool error，不让进程崩
            _log(f"tool {name} failed: {type(exc).__name__}: {exc}")
            _result(
                msg_id,
                _tool_text({"error": f"{type(exc).__name__}: {exc}"}, is_error=True),
            )
        else:
            _result(msg_id, _tool_text(payload))

    elif method == "ping":
        _result(msg_id, {})

    elif method in ("notifications/initialized", "notifications/cancelled"):
        if not is_notification:
            _result(msg_id, {})
        # 通知无需响应

    else:
        if is_notification:
            _log(f"ignore unknown notification: {method}")
        else:
            _error(msg_id, JSONRPC_METHOD_NOT_FOUND, f"method not found: {method!r}")


def main() -> int:
    settings = load_settings()
    if settings.MEMORY_NAMESPACE is None:
        _log("FATAL: MEMORY_NAMESPACE 未设置——MCP server 必须由 host 通过环境变量注入 namespace")
        return 1

    _log(f"{SERVER_NAME} {SERVER_VERSION} started, namespace={settings.MEMORY_NAMESPACE}")

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _error(None, JSONRPC_PARSE_ERROR, f"parse error: {exc}")
            continue
        if not isinstance(msg, dict):
            _error(None, JSONRPC_INVALID_REQUEST, "batch requests are not supported")
            continue
        handle_message(msg, settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
