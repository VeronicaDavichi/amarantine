"""LLM 调用（Anthropic Messages / OpenAI 双协议）。

本机接入的是 hongwang 网关（`http://192.168.0.58:3000`），走 **Anthropic Messages**
协议；`LLM_PROVIDER=openai` 时可切到 OpenAI 兼容端点。

两个实现细节值得留意：

1. **thinking 块**：deepseek-v4-flash 默认返回 `type=thinking` 的推理块
   （带 `signature`）。业务只要文本，所以必须按 `type=="text"` 过滤拼接，
   否则会把内部推理当成结果用。
2. **max_tokens 要留余量**：thinking 也消耗输出 token，给 200 会被截断成空文本。
"""

from __future__ import annotations

import httpx

from memory.config import Settings

#: Anthropic 协议版本号（网关按它路由）
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """LLM 不可用或返回结构异常。"""


def _headers(settings: Settings) -> dict[str, str]:
    key = settings.LLM_API_KEY
    if not key:
        raise LLMError("LLM_API_KEY 未配置（应放在 .env，不要提交）")
    if settings.LLM_PROVIDER == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    return {
        "Authorization": f"Bearer {key}",
        "content-type": "application/json",
    }


def _extract_text_anthropic(body: dict) -> str:
    """只取 text 块；跳过 thinking / tool_use 等非文本内容。"""
    blocks = body.get("content")
    if not isinstance(blocks, list):
        raise LLMError(f"content 字段格式异常: {type(blocks)}")
    parts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise LLMError(
            "LLM 没有返回文本（可能被 max_tokens 截断在 thinking 阶段，"
            "试增大 max_tokens）"
        )
    return text


def _extract_text_openai(body: dict) -> str:
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"OpenAI 响应结构异常: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise LLMError("LLM 返回了空文本")
    return text.strip()


def chat(
    messages: list[dict[str, str]],
    settings: Settings,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    client: httpx.Client | None = None,
    timeout_s: float | None = None,
) -> str:
    """发一轮对话，返回纯文本。

    Args:
        messages: ``[{"role": "user", "content": "..."}]``；不要放 system。
        settings: 提供 provider / base_url / model / api_key。
        system: 系统提示（Anthropic 是顶层参数，OpenAI 是首条消息）。
        max_tokens: 输出上限；带 thinking 的模型要给足，否则可能空返回。
        client: 可注入的 httpx.Client（测试用）。
        timeout_s: 超时秒数，缺省取 `LLM_TIMEOUT_MS`。

    Raises:
        LLMError: 未配置 key、网络失败、HTTP 非 200、无文本返回。
    """
    if not messages:
        raise ValueError("messages 不能为空")

    timeout = timeout_s if timeout_s is not None else settings.LLM_TIMEOUT_MS / 1000.0
    base = settings.LLM_BASE_URL.rstrip("/")
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)

    try:
        if settings.LLM_PROVIDER == "anthropic":
            payload: dict = {
                "model": settings.LLM_MODEL,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                payload["system"] = system
            url = f"{base}/v1/messages"
        else:
            payload = {
                "model": settings.LLM_MODEL,
                "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else [])
                + messages,
            }
            url = f"{base}/v1/chat/completions"

        try:
            resp = http.post(url, headers=_headers(settings), json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMError(f"LLM 响应不是合法 JSON: {exc}") from exc

        if settings.LLM_PROVIDER == "anthropic":
            return _extract_text_anthropic(body)
        return _extract_text_openai(body)
    finally:
        if owns_client:
            http.close()
