"""LLM 客户端单测（全部 mock，不打网络）。

最关键的一条：**thinking 块必须被过滤**。deepseek-v4-flash 默认返回推理块，
如果按 content[0] 取文本，拿到的是内部推理而不是答案。
"""

from __future__ import annotations

import httpx
import pytest

from memory.config import load_settings
from memory.services.llm import LLMError, chat


def _settings(**overrides):
    s = load_settings()
    return s.model_copy(update={"LLM_API_KEY": "test-key", **overrides})


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


ANTHROPIC_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "deepseek-v4-flash",
    "content": [
        {"type": "thinking", "thinking": "内部推理不应被采用", "signature": "sig"},
        {"type": "text", "text": "ok"},
    ],
    "stop_reason": "end_turn",
}


def test_extracts_text_and_skips_thinking() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=ANTHROPIC_OK)

    result = chat(
        [{"role": "user", "content": "只回复 ok"}],
        _settings(),
        client=_client(handler),
    )
    assert result == "ok"
    assert "内部推理" not in result
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "test-key"
    assert "anthropic-version" in seen["headers"]


def test_request_body_carries_model_and_system() -> None:
    import json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=ANTHROPIC_OK)

    chat(
        [{"role": "user", "content": "hi"}],
        _settings(),
        system="你是记忆助手",
        max_tokens=321,
        client=_client(handler),
    )
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["max_tokens"] == 321
    assert captured["system"] == "你是记忆助手"


def test_only_thinking_raises() -> None:
    """只有 thinking 说明被 max_tokens 截断了，必须显式报错而不是返回空串。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "thinking", "thinking": "想太多", "signature": "s"}],
                "stop_reason": "max_tokens",
            },
        )

    with pytest.raises(LLMError, match="没有返回文本"):
        chat([{"role": "user", "content": "hi"}], _settings(), client=_client(handler))


def test_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMError, match="500"):
        chat([{"role": "user", "content": "hi"}], _settings(), client=_client(handler))


def test_missing_key_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有 key 时不该发出请求")

    with pytest.raises(LLMError, match="LLM_API_KEY"):
        chat(
            [{"role": "user", "content": "hi"}],
            _settings(LLM_API_KEY=None),
            client=_client(handler),
        )


def test_network_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError, match="请求失败"):
        chat([{"role": "user", "content": "hi"}], _settings(), client=_client(handler))


def test_openai_provider_branch() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": " ok "}}]},
        )

    result = chat(
        [{"role": "user", "content": "hi"}],
        _settings(LLM_PROVIDER="openai"),
        system="sys",
        client=_client(handler),
    )
    assert result == "ok"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer test-key"


def test_empty_messages_rejected() -> None:
    with pytest.raises(ValueError, match="messages"):
        chat([], _settings())
