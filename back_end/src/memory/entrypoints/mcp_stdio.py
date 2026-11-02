"""A5 MCP stdio 入口：JSON-RPC 2.0 over stdin/stdout。

硬约束：
  - **stdout 只写协议消息**；运行日志一律落 `LOG_FILE`（见 core/logging.py），不走 std。
  - 一行一个 JSON 对象（MCP 用换行分隔的 JSON-RPC，不是 batch）。

启动：
    MEMORY_NAMESPACE=agent-a uv run memory-mcp
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from memory.config import Settings, load_settings
from memory.core.logging import fatal, setup_logging
from memory.core.tools import INSTRUCTIONS, PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, TOOLS
from memory.services.feedback import submit_feedback
from memory.services.forget import forget_event
from memory.services.recall import recall
from memory.services.recall_trace import trace_store_from_settings
from memory.services.remember import remember_event

log = logging.getLogger("memory.mcp")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(msg_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _tool_text(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """把业务结果包成 MCP tool result。"""
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    }
    if is_error:
        out["isError"] = True
    return out


# ---------- 工具处理器（M1 为桩，B/C/E 阶段逐个实现） ----------


def _tool_recall(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    """C7 `memory_recall`：检索与 query 相关的记忆材料。

    事实源不可用会抛 `RecallUnavailableError`，由外层转成 `isError`——
    MCP 没有 HTTP 状态码，调用方只能靠 isError 区分「查不到」和「查失败」。
    """
    namespace = settings.MEMORY_NAMESPACE
    if namespace is None:  # 防御性：main 已拦截
        raise RuntimeError("MEMORY_NAMESPACE 未设置")

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")

    # 传 trace_store 才会产出 trace_id —— 这是 memory_feedback 的前置凭据。
    # **但存储初始化必须容错**：trace 是辅助数据，不能让「账本写不了」
    # 连累「查记忆」这件正事。真踩过：目录不可写时整个 recall 变 isError。
    try:
        trace_store = trace_store_from_settings(settings)
    except Exception as exc:  # noqa: BLE001 - 放弃 feedback 能力，召回照常
        log.warning("引用凭据存储不可用，本次召回不带 trace_id: %s", exc)
        trace_store = None

    result = recall(
        query,
        namespace=namespace,
        settings=settings,
        trace_store=trace_store,
    )
    return result.to_payload()


def _tool_remember(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    """B1 `memory_remember`：把 LLM 的意图落成一条事件卡。

    namespace 来自进程环境（已在 main 里校验非空），不进工具参数。
    参数非法 / 下游不可用都会由外层转成 isError，这里不吞异常。
    """
    namespace = settings.MEMORY_NAMESPACE
    if namespace is None:  # 理论上 main 已拦截，防御性保留
        raise RuntimeError("MEMORY_NAMESPACE 未设置")

    result = remember_event(
        namespace=namespace,
        summary=args.get("summary"),
        date_hint=args.get("date_hint"),
        pinned=args.get("pinned", False),
        important=args.get("important", False),
        emotion_intensity=args.get("emotion_intensity", 0.0),
        context=args.get("context"),
        settings=settings,
    )
    return result.to_tool_result()


def _tool_forget(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    """D7 遗忘：软删，移出索引但保留事实层（A32）。"""
    namespace = settings.MEMORY_NAMESPACE
    if namespace is None:  # 防御性：main 已拦截
        raise RuntimeError("MEMORY_NAMESPACE 未设置")

    event_id = args.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id 必须是非空字符串")

    reason = args.get("reason", "user_forget")
    result = forget_event(
        namespace=namespace,
        event_id=event_id,
        reason=reason,
        settings=settings,
    )
    return result.to_tool_result()


def _tool_feedback(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    """E2 引用上报：告诉服务端本次召回里哪些材料真的被用了。

    `confirmed_by_use` 的**唯一**写入通道（C.B6 禁止自动判定）。
    参数不合法 → `FeedbackError` → 外层转 `isError`，让调用方自己修正重试。
    """
    namespace = settings.MEMORY_NAMESPACE
    if namespace is None:  # 防御性：main 已拦截
        raise RuntimeError("MEMORY_NAMESPACE 未设置")

    trace_id = args.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise ValueError("trace_id 必须是非空字符串")

    used = args.get("used_material_ids")
    if not isinstance(used, list) or not used:
        raise ValueError("used_material_ids 必须是非空数组")

    outcome = submit_feedback(
        namespace=namespace,
        trace_id=trace_id,
        used_material_ids=used,
        settings=settings,
    )
    return outcome.to_tool_result()


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
        log.info("initialize: namespace=%r", namespace)

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
            log.warning("拒绝工具 %s 的 namespace 入参", name)
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
            log.exception("tool %s failed", name)
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
            log.warning("忽略未知通知: %s", method)
        else:
            _error(msg_id, JSONRPC_METHOD_NOT_FOUND, f"method not found: {method!r}")


def main() -> int:
    try:
        settings = load_settings()
    except Exception as exc:  # 配置阶段失败：日志文件可能还没建
        fatal(None, f"配置加载失败: {exc}")
        return 1

    setup_logging(settings)

    if settings.MEMORY_NAMESPACE is None:
        fatal(settings, "MEMORY_NAMESPACE 未设置——MCP server 必须由 host 通过环境变量注入 namespace")
        return 1

    log.info(
        "%s %s started, namespace=%s, log=%s",
        SERVER_NAME,
        SERVER_VERSION,
        settings.MEMORY_NAMESPACE,
        settings.LOG_FILE,
    )

    # 用图上的已有名字预热 jieba 词典。
    #
    # **必须在这里做，不能每次写入后做**：反哺会改变下一次的分词结果，
    # 而 `event_id` 的哈希里有 `entity_names` —— 同一句话写两次会得到
    # 两个不同的 id，一条记忆裂成两条。启动时灌一次就没有这个问题：
    # 同一个进程内词典固定，切分必然一致。
    from memory.services.remember import warm_up_dictionary

    warmed = warm_up_dictionary(settings.MEMORY_NAMESPACE, settings=settings)
    log.info("分词词典预热完成，新增 %d 个词", warmed)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("parse error: %s", exc)
            _error(None, JSONRPC_PARSE_ERROR, f"parse error: {exc}")
            continue
        if not isinstance(msg, dict):
            _error(None, JSONRPC_INVALID_REQUEST, "batch requests are not supported")
            continue
        handle_message(msg, settings)

    log.info("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
