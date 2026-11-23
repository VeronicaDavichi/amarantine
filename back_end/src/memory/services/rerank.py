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


#: 最近一次 rerank 的结果，供 `/health` 暴露。
#:
#: **为什么需要**：降级是设计好的（不让召回 500），但它一直是**静默**的 ——
#: 实测 `reason=http_413` 持续了几周，只有翻 trace 才发现精排从未生效。
#: 把最近结果与连续降级次数记在模块级，`/health` 一问就知道。
_LAST: dict[str, object] = {"ok": None, "reason": "", "elapsed_ms": 0}
_CONSECUTIVE_FAILURES = 0
#: 连续降级多少次后在同一条日志里给「怎么办」（避免每次查询刷屏）
_LOUD_AFTER = 3
_louded = False


def last_outcome() -> dict[str, object]:
    """最近一次 rerank 结果（`/health` 用）。"""
    return {**_LAST, "consecutive_failures": _CONSECUTIVE_FAILURES}


def _record(*, ok: bool, reason: str, elapsed_ms: int) -> None:
    global _CONSECUTIVE_FAILURES, _louded
    _LAST.update({"ok": ok, "reason": reason, "elapsed_ms": elapsed_ms})
    if ok:
        _CONSECUTIVE_FAILURES = 0
        _louded = False
        return
    _CONSECUTIVE_FAILURES += 1
    if _CONSECUTIVE_FAILURES == _LOUD_AFTER and not _louded:
        _louded = True
        logger.warning(
            "rerank 连续 %d 次降级（reason=%s）—— 精排当前**从未生效**。"
            "排查顺序：① 服务 batch 上限是否 >= RERANK_CANDIDATES（TEI 报 413 就是它）"
            "② RERANK_TIMEOUT_MS 是否够（CPU 上约 120ms/条）"
            "③ 是否该把 RERANK_ENABLED 关掉",
            _CONSECUTIVE_FAILURES, reason,
        )

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
            reason = f"http_{resp.status_code}"
            # 413 在 TEI 里**几乎总是 batch 上限**，不是请求体太大。
            # 踩过：服务端 `--max-client-batch-size 4` 而这里送 50 → 每次召回都
            # 413 静默降级；因为 reasons 只记 `http_413`，看起来像「请求太大」，
            # 排查时没人会想到是自己 compose 里写死的 4。
            if resp.status_code == 413 and "batch size" in resp.text.lower():
                reason = "http_413_batch_limit"
                logger.warning(
                    "rerank 被服务端的 batch 上限拒绝（%s）。"
                    "把服务端的 --max-client-batch-size 提到 >= %d，"
                    "或把 RERANK_CANDIDATES 降到上限以内。",
                    resp.text[:120], len(candidates),
                )
            elapsed = int((time.monotonic() - started) * 1000)
            _record(ok=False, reason=reason, elapsed_ms=elapsed)
            return RerankResult(scores=None, elapsed_ms=elapsed, reason=reason)
        data = resp.json()
        mapping: dict[str, float] = {}
        for item in data:
            idx = int(item["index"])
            if 0 <= idx < len(candidates):
                mapping[candidates[idx][0]] = float(item["score"])
        if not mapping:
            _record(ok=False, reason="empty_response", elapsed_ms=0)
            return RerankResult(scores=None, elapsed_ms=0, reason="empty_response")
        elapsed = int((time.monotonic() - started) * 1000)
        _record(ok=True, reason="", elapsed_ms=elapsed)
        return RerankResult(scores=mapping, elapsed_ms=elapsed)
    except Exception as exc:  # noqa: BLE001 - 降级是设计，不是兜底
        logger.warning("rerank 不可用，已降级：%s", exc)
        elapsed = int((time.monotonic() - started) * 1000)
        _record(ok=False, reason=type(exc).__name__, elapsed_ms=elapsed)
        return RerankResult(
            scores=None, elapsed_ms=elapsed, reason=type(exc).__name__
        )
    finally:
        if owns_client:
            http.close()

def startup_check(settings: Settings) -> list[str]:
    """启动时检查 rerank 配置是否自相矛盾，返回告警列表（空 = 没问题）。

    ## 为什么要检查

    这组配置互相约束，写错时**完全静默**：

        RERANK_ENABLED=true
        RERANK_CANDIDATES=50      ← 每次送 50 条
        RERANK_TIMEOUT_MS=800     ← 但 CPU 上约 120ms/条 → 至少 6 秒

    结果是每次召回都 `ReadTimeout` 降级，召回照常 200，没人会发现。
    同类问题还有「服务端 batch 上限 < RERANK_CANDIDATES」→ 每次 413。

    这里按 CPU 实测的 ~120ms/条做**保守**估计（GPU 会快一个数量级，
    所以只警告不阻断）。
    """
    out: list[str] = []
    if not settings.RERANK_ENABLED:
        return out
    # CPU 实测（bge-reranker-v2-m3）：n=1 253ms / n=4 572ms / n=8 1100ms
    # → 约 120ms/条 + 130ms 固定开销。GPU 会快一个数量级，所以只警告不阻断。
    per_item_ms = 120.0
    fixed_ms = 130.0
    implied = fixed_ms + settings.RERANK_CANDIDATES * per_item_ms
    # 该超时预算内**实测可排**的条数（扣掉固定开销）
    safe_n = max(1, int((settings.RERANK_TIMEOUT_MS - fixed_ms) / per_item_ms))
    if implied > settings.RERANK_TIMEOUT_MS * 2:
        out.append(
            f"RERANK_CANDIDATES={settings.RERANK_CANDIDATES} × ~{per_item_ms:.0f}ms/条 "
            f"+ {fixed_ms:.0f}ms ≈ {implied / 1000:.1f}s，"
            f"远超 RERANK_TIMEOUT_MS={settings.RERANK_TIMEOUT_MS}ms"
            f" —— 每次召回都会超时降级（精排不会生效）。"
            f" 按实测该预算只能排约 {safe_n} 条；要么把 RERANK_CANDIDATES 降到 "
            f"{safe_n} 以内，要么加 RERANK_TIMEOUT_MS，要么关掉 RERANK_ENABLED。"
        )
    if implied > settings.RECALL_BUDGET_MS:
        out.append(
            f"rerank 估算耗时 {implied / 1000:.1f}s 超过召回总预算 "
            f"RECALL_BUDGET_MS={settings.RECALL_BUDGET_MS}ms —— "
            f"会把其他通道的等待时间挤掉。"
        )
    return out

