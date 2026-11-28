# -*- coding: utf-8 -*-
"""图片视觉概括（spec 056）：把一张图变成一句中文描述。

## 为什么需要

记忆里存的是「[图片] <hash>.png」这种占位符 —— 对召回、摘要、关系抽取
全都是噪声。但图片的**语义**往往正是这条消息的重点（"你看这个"）。

在**实时入口**把图当场概括成一句中文，写回 `raw_quote.text`，
下游四层（切分/摘要/向量/BM25）就完全不需要知道「有图片这回事」。

## 设计约束（都来自 spec 056）

1. **绝不抛异常**：任何失败返回 `None`，调用方写占位符。
   概括是增强项，丢消息才是事故。
2. **支持三种来源**：URL / 本地路径 / 直接给 bytes。
   实时网关给的形态不统一，上层不该为此分三套代码。
3. **大小上限**：超过 `MAX_IMAGE_BYTES` 直接放弃（不做缩放 —— 项目没装 Pillow，
   引一个图像库只为缩图不值得）。宁可不概括，也不要拿 4MB 图去打爆 LLM。
4. **带上下文**：只给一张图，模型只能猜；给上前几条消息，
   它才知道"这个"指的是什么。
"""

from __future__ import annotations

import base64
import logging
import pathlib
import urllib.request

from memory.config import Settings
from memory.services.llm import LLMError, chat

logger = logging.getLogger("memory.image_caption")

#: 单张图大小上限。超过就不概括（降级占位符）。
#: 4MB base64 后约 5.3MB 请求体，多数网关能吃下；再大风险收益比不划算。
MAX_IMAGE_BYTES = 4 * 1024 * 1024

#: 支持的类型 → MIME。QQ/OneBot 常见格式就这些。
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: 概括提示词。要短、要具体、不许编造。
_PROMPT = """你在帮一个长期记忆系统记录聊天里的图片。请用一句中文（不超过 40 字）描述这张图。

要求：
- 只写**能直接看到**的内容：主体、动作、场景、明显的文字
- 如果是表情包/贴纸，写「表情包：<大意>」
- 如果是商品图/公告图，写清品类与关键文字
- 如果是截图，写清是什么界面、关键数字
- **不要猜测**看不见的东西（图里没有人的情绪、拍摄动机都不要写）
- 直接输出描述，不要「这张图显示了」这种废话开头

{context}"""


def _mime_of(name: str, fallback: str = "image/jpeg") -> str:
    return _EXT_MIME.get(pathlib.Path(str(name)).suffix.lower(), fallback)


def load_image(source: str | bytes) -> tuple[bytes, str] | None:
    """取图 → `(bytes, mime)`；拿不到返回 None。

    Args:
        source: `http(s)://…` / 本地绝对路径 / 已经是图片字节。

    Note:
        超限、读不到、格式不认识都返回 None —— 不抛，让调用方继续走降级。
    """
    if isinstance(source, bytes):
        return (source, "image/jpeg") if source else None
    raw = (source or "").strip()
    if not raw:
        return None

    if raw.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(raw, timeout=15) as resp:
                data = resp.read(MAX_IMAGE_BYTES + 1)
                ctype = resp.headers.get("Content-Type") or ""
        except Exception as exc:  # noqa: BLE001 - URL 过期/网络问题都是常态
            logger.warning("取图失败（URL）: %s", type(exc).__name__)
            return None
        if len(data) > MAX_IMAGE_BYTES:
            logger.warning("图片过大（%d B），跳过概括", len(data))
            return None
        mime = ctype.split(";")[0].strip() or _mime_of(raw)
        return data, mime if mime.startswith("image/") else _mime_of(raw)

    path = pathlib.Path(raw)
    if not path.is_file():
        logger.warning("取图失败（文件不存在）: %s", raw)
        return None
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            logger.warning("图片过大（%d B），跳过概括: %s",
                           path.stat().st_size, path.name)
            return None
        return path.read_bytes(), _mime_of(path.name)
    except OSError as exc:
        logger.warning("读图失败: %s", type(exc).__name__)
        return None


def _content_blocks(data: bytes, mime: str, prompt: str, provider: str) -> list[dict]:
    """按 provider 拼 content blocks（两家格式不同）。"""
    b64 = base64.b64encode(data).decode("ascii")
    if provider == "anthropic":
        return [
            {"type": "image",
             "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": prompt},
        ]
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]


def _context_block(context: list[str] | None) -> str:
    """把前几条消息拼成「上下文」段（没有就空串）。"""
    lines = [str(c).strip() for c in (context or []) if str(c or "").strip()]
    if not lines:
        return ""
    joined = "\n".join(f"- {x[:80]}" for x in lines[-3:])
    return f"上下文（这几条消息在同一段对话里，帮你判断「这个/那个」指什么）：\n{joined}\n"


def caption(
    source: str | bytes,
    *,
    settings: Settings,
    context: list[str] | None = None,
    timeout_s: float | None = None,
) -> str | None:
    """概括一张图，返回一句中文；任何失败返回 None。

    Args:
        source: 图片 URL / 本地路径 / 字节。
        settings: 提供 LLM 配置；`IMAGE_CAPTION_ENABLED` 关时直接返回 None。
        context: 前几条消息（帮模型判断指代）。
        timeout_s: 覆盖 LLM 超时。

    Returns:
        描述文本（已 strip、去换行），失败为 None。
    """
    if not getattr(settings, "IMAGE_CAPTION_ENABLED", False):
        return None

    got = load_image(source)
    if got is None:
        return None
    data, mime = got

    prompt = _PROMPT.format(context=_context_block(context))
    messages = [{"role": "user",
                 "content": _content_blocks(data, mime, prompt, settings.LLM_PROVIDER)}]
    try:
        text = chat(
            messages,
            settings,
            max_tokens=int(getattr(settings, "IMAGE_CAPTION_MAX_TOKENS", 320)),
            timeout_s=timeout_s
            or float(getattr(settings, "IMAGE_CAPTION_TIMEOUT_MS", 60000)) / 1000.0,
        )
    except LLMError as exc:
        logger.warning("视觉概括失败（降级为占位符）: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 绝不抛给调用方
        logger.warning("视觉概括异常（降级为占位符）: %s", type(exc).__name__)
        return None

    out = " ".join((text or "").split())
    if not out:
        return None
    # 模型偶尔会加前缀，剥掉（提示词里要求了，但别指望它总听话）
    for prefix in ("这张图显示了", "这张图片显示了", "图片中", "图中", "描述："):
        if out.startswith(prefix):
            out = out[len(prefix):].lstrip("：: ")
    return out[:200] or None


def render_marker(caption_text: str | None, fallback: str = "[图片]") -> str:
    """生成写进 `raw_quote.text` 的占位文本。

    - 概括成功 → `[图片：<描述>]`
    - 失败     → `[图片]`（原 fallback，可能带 hash 与扩展名）
    """
    text = (caption_text or "").strip()
    return f"[图片：{text}]" if text else fallback
