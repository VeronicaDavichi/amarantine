"""Embedding 调用（内部服务，OpenAI 兼容）。

格式由框架决定（ADR「不做清单」明确不对外承诺），P0 对接 TEI。

**注意**：TEI 1.8 的字段是 ``input``，不是 1.7 的 ``inputs``。
写死成一种会导致升级后 422，所以在客户端里统一收口。
"""

from __future__ import annotations

import httpx

from memory.config import Settings


class EmbeddingError(RuntimeError):
    """embedding 服务不可用或返回不合规，写入链路应转 503。"""


def embed_texts(
    texts: list[str],
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """批量编码。

    Args:
        texts: 待编码文本，不能为空。
        settings: 提供 base_url / model / dim。
        client: 可注入的 httpx.Client（测试用）。
        timeout_s: 超时秒数。

    Returns:
        与 ``texts`` 等长的向量列表。

    Raises:
        EmbeddingError: 服务不可达、HTTP 非 200、维度不符或数量不匹配。
    """
    if not texts:
        raise ValueError("texts 不能为空")

    payload = {"input": texts, "model": settings.EMBEDDING_MODEL}
    url = f"{settings.EMBEDDING_BASE_URL.rstrip('/')}/v1/embeddings"
    # `EMBEDDING_API_KEY` 是给「远端 embedding」用的（高低配混合部署：模型在高配机）。
    # 为空时不发 header —— 本地服务不需要，也别让空 Bearer 去触发别人的鉴权分支。
    headers: dict[str, str] = {}
    if settings.EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {settings.EMBEDDING_API_KEY}"

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout_s)
    try:
        try:
            resp = http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding 服务不可达: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingError(
                f"embedding 返回 {resp.status_code}: {resp.text[:200]}"
            )

        try:
            body = resp.json()
            items = body["data"]
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding 响应结构异常: {exc}") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"向量数量不匹配：期望 {len(texts)}，实际 {len(vectors)}"
            )
        for vec in vectors:
            if len(vec) != settings.EMBEDDING_DIM:
                raise EmbeddingError(
                    f"向量维度不匹配：期望 {settings.EMBEDDING_DIM}，实际 {len(vec)}"
                    "（换模型后必须同步 EMBEDDING_DIM 与 collection 维度）"
                )
        return vectors
    finally:
        if owns_client:
            http.close()


def embed_one(text: str, settings: Settings, **kwargs) -> list[float]:
    """单条编码的便捷封装。"""
    return embed_texts([text], settings, **kwargs)[0]
