"""A27：rerank 精排 + 降级路径。

ADR 原文：kill rerank 服务，`POST /admin/recall` 仍返回 200，
`trace.rerank_skipped=true`、`trace.rerank_ms=0`，材料集非空。

**这条的关键不是「调通 rerank」，是「rerank 挂了不能拖垮召回」。**
rerank 是增强项：它让排序更准，但不该成为可用性的单点。

服务形态：`text-embeddings-inference` 的 `/rerank` 端点，
请求 `{"query": str, "texts": [str], "model": str}`，响应 `[{"index": int, "score": float}]`。

**注意**：TEI 实例只会用它 `--model-id` 加载的那个模型做 rerank。
用 embedding 模型（如 bge-m3）调 `/rerank` 会返回 424
`model is not a re-ranker model` —— 我们把它归成「不可用」，走降级。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from memory.config import Settings

logger = logging.getLogger(__name__)


class RerankResult:
    """一次 rerank 的产物。`scores is None` 表示不可用（已降级）。"""

    __slots__ = ("scores", "skipped", "elapsed_ms", "reason")

    def __init__(
        self,
        *,
        scores: dict[str, float] | None,
        elapsed_ms: int,
        reason: str | None = None,
    ) -> None:
        self.scores = scores
        self.skipped = scores is None
        # ADR 要求：跳过时 `rerank_ms` 必须是 0，不能是「尝试花掉的时间」
        self.elapsed_ms = 0 if self.skipped else elapsed_ms
        self.reason = reason

    def to_trace(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rerank_skipped": self.skipped,
            "rerank_ms": self.elapsed_ms,
        }
        if self.reason:
            out["rerank_reason"] = self.reason
        return out


def rerank(
    query: str,
    candidates: list[tuple[str, str]],
    *,
    settings: Settings,
    client: httpx.Client | None = None,
) -> RerankResult:
    """对候选做精排，返回按分数降序的 `event_id` 列表。

    Args:
        query: 用户查询。
        candidates: `[(event_id, text)]`，text 用于打分（通常是 summary）。
        settings: 提供 `RERANK_*` 配置。
        client: 可注入的 httpx.Client（测试用）。

    Returns:
        `RerankResult`。**任何失败都不抛** —— 降级由 `scores=None` 表达。
        这是刻意设计：rerank 挂了不该让召回 500。
    """
    if not settings.RERANK_ENABLED:
        return RerankResult(scores=None, elapsed_ms=0, reason="disabled")
    if not candidates:
        return RerankResult(scores={}, elapsed_ms=0, reason="no_candidates")

    texts = [text for _, text in candidates]
    payload = {
        "query": query,
        "texts": texts,
        "model": settings.RERANK_MODEL,
    }
    url = f"{settings.RERANK_BASE_URL.rstrip('/')}/rerank"
    timeout_s = max(settings.RERANK_TIMEOUT_MS / 1000.0, 0.05)
    # 空 key 不发 header：本地服务不校验，发个空 Bearer 只会让日志变脏
    headers: dict[str, str] = {}
    if getattr(settings, "RERANK_API_KEY", None):
        headers["Authorization"] = f"Bearer {settings.RERANK_API_KEY}"

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout_s)
    started = time.monotonic()
    try:
        resp = http.post(url, json=payload, timeout=timeout_s, headers=headers)
        if resp.status_code != 200:
            # 424（模型不是 reranker）也走这里 —— 服务「在」但「不可用」
            return RerankResult(
                scores=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                reason=f"http_{resp.status_code}",
            )
        data = resp.json()
        mapping: dict[str, float] = {}
        for item in data:
            idx = int(item["index"])
            if 0 <= idx < len(candidates):
                mapping[candidates[idx][0]] = float(item["score"])
        if not mapping:
            return RerankResult(scores=None, elapsed_ms=0, reason="empty_response")
        return RerankResult(
            scores=mapping,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 降级是设计，不是兜底
        logger.warning("rerank 不可用，已降级：%s", exc)
        return RerankResult(
            scores=None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            reason=type(exc).__name__,
        )
    finally:
        if owns_client:
            http.close()
