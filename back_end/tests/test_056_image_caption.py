"""spec 056：视觉概括服务 + 实时入口。

## 测试策略

- 概括服务：**不真调 LLM**，用 monkeypatch 替换 `chat`，覆盖成功/失败/超限/类型
- 实时入口：直接测端点函数（FastAPI 依赖注入绕过），用临时 SQLite
"""

from __future__ import annotations

import pathlib

import pytest

from memory.services import image_caption as cap


class _S:
    IMAGE_CAPTION_ENABLED = True
    IMAGE_CAPTION_MAX_TOKENS = 1500
    IMAGE_CAPTION_TIMEOUT_MS = 60000
    LLM_PROVIDER = "anthropic"
    LLM_MODEL = "deepseek-v4-flash"
    LLM_BASE_URL = "http://127.0.0.1:1"
    LLM_API_KEY = "k"


@pytest.fixture()
def png(tmp_path) -> pathlib.Path:
    """最小合法 PNG（1x1 透明）。"""
    import base64

    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    p = tmp_path / "t.png"
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------- 取图


def test_load_image_from_path(png) -> None:
    got = cap.load_image(str(png))
    assert got is not None
    data, mime = got
    assert data.startswith(b"\x89PNG")
    assert mime == "image/png"


def test_load_image_from_bytes() -> None:
    got = cap.load_image(b"\x89PNG\r\n\x1a\n")
    assert got is not None


def test_load_image_missing_file(tmp_path) -> None:
    assert cap.load_image(str(tmp_path / "nope.png")) is None


def test_load_image_too_large(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cap, "MAX_IMAGE_BYTES", 10)
    p = tmp_path / "big.png"
    p.write_bytes(b"x" * 100)
    assert cap.load_image(str(p)) is None


def test_load_image_bad_url() -> None:
    assert cap.load_image("http://127.0.0.1:1/x.png") is None


# ---------------------------------------------------------------- 概括


def test_caption_success(png, monkeypatch) -> None:
    monkeypatch.setattr(cap, "chat", lambda *a, **kw: "一张橘猫趴在键盘上")
    assert cap.caption(str(png), settings=_S()) == "一张橘猫趴在键盘上"


def test_caption_strips_prefix(png, monkeypatch) -> None:
    """模型偶尔加「这张图显示了」这种前缀，要剥掉。"""
    monkeypatch.setattr(cap, "chat", lambda *a, **kw: "图中：一只狗在跑")
    assert cap.caption(str(png), settings=_S()) == "一只狗在跑"


def test_caption_disabled(png, monkeypatch) -> None:
    class _Off(_S):
        IMAGE_CAPTION_ENABLED = False

    called = []
    monkeypatch.setattr(cap, "chat", lambda *a, **kw: called.append(1) or "x")
    assert cap.caption(str(png), settings=_Off()) is None
    assert called == [], "关掉时不该调模型"


def test_caption_llm_error_returns_none(png, monkeypatch) -> None:
    from memory.services.llm import LLMError

    def boom(*a, **kw):
        raise LLMError("超时")

    monkeypatch.setattr(cap, "chat", boom)
    assert cap.caption(str(png), settings=_S()) is None


def test_caption_unexpected_error_returns_none(png, monkeypatch) -> None:
    def boom(*a, **kw):
        raise ValueError("模型抽风")

    monkeypatch.setattr(cap, "chat", boom)
    assert cap.caption(str(png), settings=_S()) is None, "任何异常都不许抛给调用方"


def test_caption_empty_response(png, monkeypatch) -> None:
    monkeypatch.setattr(cap, "chat", lambda *a, **kw: "   ")
    assert cap.caption(str(png), settings=_S()) is None


def test_caption_includes_context(png, monkeypatch) -> None:
    captured = {}

    def fake(messages, *a, **kw):
        captured["blocks"] = messages[0]["content"]
        return "描述"

    monkeypatch.setattr(cap, "chat", fake)
    cap.caption(str(png), settings=_S(), context=["他说你看这个", "然后就发了图"])
    text_blocks = [b for b in captured["blocks"] if b.get("type") == "text"]
    assert text_blocks and "他说你看这个" in text_blocks[0]["text"]


def test_caption_anthropic_block_shape(png, monkeypatch) -> None:
    captured = {}

    def fake(messages, *a, **kw):
        captured["blocks"] = messages[0]["content"]
        return "描述"

    monkeypatch.setattr(cap, "chat", fake)
    cap.caption(str(png), settings=_S())
    img = next(b for b in captured["blocks"] if b.get("type") == "image")
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"


def test_caption_openai_block_shape(png, monkeypatch) -> None:
    class _OA(_S):
        LLM_PROVIDER = "openai"

    captured = {}

    def fake(messages, *a, **kw):
        captured["blocks"] = messages[0]["content"]
        return "描述"

    monkeypatch.setattr(cap, "chat", fake)
    cap.caption(str(png), settings=_OA())
    img = next(b for b in captured["blocks"] if b.get("type") == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------- 占位符


def test_render_marker_caption() -> None:
    assert cap.render_marker("一只猫") == "[图片：一只猫]"


def test_render_marker_fallback() -> None:
    assert cap.render_marker(None, "[图片] abc.jpg") == "[图片] abc.jpg"
    assert cap.render_marker("", "[图片]") == "[图片]"


def test_mime_unknown_extension() -> None:
    assert cap._mime_of("x.bmp") == "image/jpeg", "不认识的扩展名退回 jpeg"
