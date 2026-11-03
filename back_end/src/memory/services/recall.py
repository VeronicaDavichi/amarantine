"""C1/C5/C7 `memory_recall` 编排。

ADR D3 的三条硬约束，这里逐条落地：

**并行扇出**：三路并发，不串行。串行两跳的 P99 不可控，且错误会级联。

**超时降级矩阵**：

| 通道 | 超时 | 失败处理 |
|---|---|---|
| Weaviate | 800ms | 整体失败 → 503 |
| Neo4j | 200ms | 丢弃该路，`degraded=true` |
| 整体 | `budget_ms` | 返回已就绪部分，`partial=true` |

`degraded` 与 `partial` 必须**分开上报**：调用方据此决定「重试」还是「降级展示」。

**空结果显式**：200 + `materials: []` + `trace.empty_reason`，
绝不用低分材料凑数，也绝不返回 404（调用方要能区分「没有」和「失败」）。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from memory.config import Settings
from memory.core.dynamic_k import Candidate, decide_k
from memory.core.rrf import rrf_fuse
from memory.core.scoring import normalize_relevance, weighted_score
from memory.core.time_resolver import DateRange, resolve_from_query
from memory.services.embedding import EmbeddingError
from memory.services.graph_search import search_by_entities
from memory.services.labeler import extract_entities
from memory.services.rerank import rerank
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger("memory.recall")

#: Weaviate 通道超时（ADR D3）；超时即整体 503
WEAVIATE_TIMEOUT_MS = 800
#: 整体预算上限；耗尽则返回部分结果
DEFAULT_BUDGET_MS = 1500


class RecallUnavailableError(RuntimeError):
    """事实源不可用——调用方应返回 503，而不是空结果。"""


@dataclass
class RecallResult:
    """召回结果。`materials` 为空时必须带 `empty_reason`。"""

    materials: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    partial: bool = False
    trace: dict[str, Any] = field(default_factory=dict)
    #: 本次召回的引用凭据。`memory_feedback` 靠它校验「是不是真的返回过」。
    #: 空结果也给 —— 否则 LLM 无法上报「一条都没用」。
    trace_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "materials": self.materials,
            "degraded": self.degraded,
            "partial": self.partial,
            "trace": self.trace,
            "trace_id": self.trace_id,
        }


def recall(
    query: str,
    *,
    namespace: str,
    settings: Settings,
    now: datetime | None = None,
    today: date | None = None,
    store: WeaviateStore | None = None,
    embedder: Any | None = None,
    graph_searcher: Any | None = None,
    counter: Any | None = None,
    budget_ms: int | None = None,
    trace_store: Any | None = None,
    rerank_client: Any | None = None,
) -> RecallResult:
    """召回与 query 相关的记忆材料。

    Args:
        query: 用户查询（可含时间词，会被摘出来解析）。
        namespace: 记忆域（强制参与所有通道的过滤）。
        settings: 配置。
        now: 当前时刻，缺省系统 UTC。
        today: 「今天」基准日，缺省取 now 的日期。
        store: 可注入的 Weaviate 封装。
        embedder: 可替换的编码函数 ``(text, settings) -> list[float]``。
        graph_searcher: 可替换的图通道函数。
        counter: 召回计数器（`RecallCounter`）；None 表示不回写。
        budget_ms: 整体预算，缺省取 `RECALL_BUDGET_MS`。
        trace_store: 引用凭据存储（`RecallTraceStore`）。传了才会产出
            `trace_id` 并落快照；None 表示不需要 feedback 能力（测试/内部调用）。

    Raises:
        RecallUnavailableError: 事实源（Weaviate / embedding）不可用 → 503。
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")

    moment = now or datetime.now(timezone.utc)
    base_day = today or moment.date()
    budget = budget_ms if budget_ms is not None else settings.RECALL_BUDGET_MS

    # 1) 时间意图（规则层；解析不出就给全库）
    date_range: DateRange = resolve_from_query(query, today=base_day)
    date_from = None
    if date_range.confidence > 0 and date_range.end is not None:
        date_from = f"{date_range.start.isoformat()}T00:00:00Z"

    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    candidates_limit = settings.RECALL_CANDIDATES

    try:
        # 2) query 向量（事实源挂 → 503，不是空结果）
        encode = embedder or _default_embedder
        try:
            query_vec = encode(query, settings)
        except EmbeddingError as exc:
            raise RecallUnavailableError(f"embedding 不可用: {exc}") from exc

        # 3) 并行扇出
        #    人跟实体要分开传：人和实体在图通道走不同的路
        #    （实体命中后会挖动作和相关人，人则直接收工，见 graph_search 文档）
        _query_ents = extract_entities(query)
        persons = [e.name for e in _query_ents if e.kind == "person"]
        entities = [e.name for e in _query_ents if e.kind != "person"]
        graph_fn = graph_searcher or search_by_entities

        def run_vector():
            return active_store.search_near_vector(
                query_vec, namespace, limit=candidates_limit, date_from=date_from
            )

        def run_bm25():
            return active_store.search_bm25(
                query, namespace, limit=candidates_limit, date_from=date_from
            )

        def run_graph():
            return graph_fn(
                entities, namespace, person_names=persons, settings=settings
            )

        channel_results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "vector": pool.submit(run_vector),
                "bm25": pool.submit(run_bm25),
                "graph": pool.submit(run_graph),
            }
            remaining = budget / 1000.0
            partial = False
            for name, future in futures.items():
                try:
                    channel_results[name] = future.result(timeout=max(0.01, remaining))
                except FutureTimeout:
                    partial = True
                    errors[name] = "budget exhausted"
                    future.cancel()
                except WeaviateStoreError as exc:
                    # Weaviate 是事实源：挂了必须 503，不能当空结果
                    if name in ("vector", "bm25"):
                        raise RecallUnavailableError(f"{name} 通道失败: {exc}") from exc
                    errors[name] = str(exc)
                except Exception as exc:  # noqa: BLE001
                    errors[name] = f"{type(exc).__name__}: {exc}"
                    logger.warning("通道 %s 失败: %s", name, exc)

        # 4) 图通道失败只降级
        degraded = False
        graph_hits = channel_results.get("graph")
        if graph_hits is None or not getattr(graph_hits, "ok", True):
            degraded = True

        # 5) 组装通道排名（空通道直接不放进去——ADR：移除而非补偿）
        #
        # ⚠️ 这里必须先做**绝对相似度闸门**，再交给 RRF。
        # 原因：RRF 只用排名，`normalize_relevance` 又把 top-1 归一成 1.0，
        # 于是「矮子里拔将军」的候选也会拿到接近满分的融合分。绝对余弦是
        # 唯一还能区分「真的像」和「都不像」的信号，只能在融合前用。
        channels: dict[str, list[str]] = {}
        vector_hits = channel_results.get("vector") or []
        bm25_hits = channel_results.get("bm25") or []
        sim_floor = settings.RECALL_VECTOR_MIN_SIM
        kept_vector = [
            h for h in vector_hits
            if h.get("event_id") and float(h.get("score") or 0.0) >= sim_floor
        ]
        dropped_vector = len(vector_hits) - len(kept_vector)
        # BM25 分没有上界，>0 就表示真的命中过词；不设额外阈值
        kept_bm25 = [
            h for h in bm25_hits
            if h.get("event_id") and float(h.get("score") or 0.0) > 0.0
        ]
        if kept_vector:
            channels["vector"] = [h["event_id"] for h in kept_vector]
        if kept_bm25:
            channels["bm25"] = [h["event_id"] for h in kept_bm25]
        # 图通道是实体扩展，天然带有相关性假设，不额外设闸
        if graph_hits is not None and getattr(graph_hits, "event_ids", None):
            channels["graph"] = list(graph_hits.event_ids)

        # 通道被闸门清空 → 是「有候选但都不够像」，不是「完全没有」
        gate_filtered = bool(dropped_vector) and not channels
        # 闸门诊断始终上报：排查「为什么召回不到」时第一个要看的就是它
        gate_trace = {
            "vector_min_sim": sim_floor,
            "vector_candidates": len(vector_hits),
            "vector_dropped": dropped_vector,
            "bm25_candidates": len(bm25_hits),
        }
        if not channels:
            return _with_trace(
                RecallResult(
                    materials=[],
                    degraded=degraded,
                    partial=partial,
                    trace={
                        "empty_reason": (
                            "below_threshold" if gate_filtered else "no_candidate"
                        ),
                        "channels": {},
                        "errors": errors,
                        "date_range": _range_dict(date_range),
                        "gate": gate_trace,
                    },
                ),
                trace_store=trace_store, namespace=namespace, query=query,
            )

        # 6) RRF 融合
        ranked = rrf_fuse(channels)
        # 元数据索引：只有 vector/bm25 带回完整属性，图通道只给 id
        # 用 kept_* —— 被闸门丢掉的不该再提供元数据（否则图通道会把它救回来）
        meta: dict[str, dict[str, Any]] = {}
        for hit in [*kept_bm25, *kept_vector]:
            eid = hit.get("event_id")
            if eid and eid not in meta:
                meta[eid] = hit

        # 7) 打分：RRF 归一 → 时效 × 重要性
        ranked = [(eid, s) for eid, s in ranked if eid in meta]
        if not ranked:
            return _with_trace(
                RecallResult(
                    materials=[],
                    degraded=degraded,
                    partial=partial,
                    trace={
                        "empty_reason": "no_candidate",
                        "channels": {k: len(v) for k, v in channels.items()},
                        "errors": errors,
                        "note": "图通道命中的 id 没有元数据（P0 限制）",
                    },
                ),
                trace_store=trace_store, namespace=namespace, query=query,
            )

        norm = normalize_relevance([s for _, s in ranked])
        scored: list[Candidate] = []
        for (eid, _), rel in zip(ranked, norm):
            item = meta[eid]
            delta_days = _days_since(item.get("date_start"), base_day)
            final = weighted_score(
                rel,
                delta_days=delta_days,
                importance=_clamp01(item.get("importance")),
                w_time_base=settings.W_TIME_BASE,
                w_imp_base=settings.W_IMP_BASE,
                half_life_days=settings.RECENCY_HALF_LIFE_DAYS,
            )
            forced = bool(item.get("pinned")) or bool(item.get("unresolved"))
            scored.append(
                Candidate(
                    event_id=eid,
                    score=final,
                    tokens=_estimate_tokens(item.get("summary") or ""),
                    forced=forced,
                )
            )

        scored.sort(key=lambda c: (-c.score, c.event_id))

        # 8) 空结果判定（ADR：最高分低于阈值也算空）
        top = scored[0].score if scored else 0.0
        if not scored or top < settings.RECALL_EMPTY_THRESHOLD:
            return _with_trace(
                RecallResult(
                    materials=[],
                    degraded=degraded,
                    partial=partial,
                    trace={
                        "empty_reason": "below_threshold",
                        "top_score": round(top, 4),
                        "threshold": settings.RECALL_EMPTY_THRESHOLD,
                        "channels": {k: len(v) for k, v in channels.items()},
                        "errors": errors,
                    },
                ),
                trace_store=trace_store, namespace=namespace, query=query,
            )

        # 8.5) rerank 精排（A27）
        #
        # **位置**：RRF 融合之后、动态 K 截断之前 —— 截断后再 rerank 没有意义，
        # 候选已经丢了。
        #
        # **失败不抛**：rerank 是增强项，挂了就退回 RRF 顺序继续，整体仍是 200。
        # 这是 ADR A27 的核心断言（kill 服务后材料集非空 + trace 标 skipped）。
        rerank_result = rerank(
            query,
            [
                (c.event_id, str(meta.get(c.event_id, {}).get("summary") or ""))
                for c in scored
            ],
            settings=settings,
            client=rerank_client,
        )
        if not rerank_result.skipped and rerank_result.scores:
            # **必须换分数，改 list 顺序没用。**
            # `decide_k` 的 docstring 明说「顺序不限，本函数自己排」，
            # 而 `Candidate` 是 frozen —— 所以只能 replace 出一个新候选。
            #
            # 代价：阈值（min_score / drop_ratio / gap_ratio）之后是在
            # **rerank 分的尺度**上判断，不再是 RRF 分。这是 rerank 生效的
            # 必然结果 —— 两套分数不能混用于同一个排序，只能整套换掉。
            scored = [
                replace(c, score=rerank_result.scores[c.event_id])
                if c.event_id in rerank_result.scores
                else c
                for c in scored
            ]

        # 9) 动态 K
        decision = decide_k(
            scored,
            budget_tokens=settings.CONTEXT_TOKEN_BUDGET,
            min_k=settings.RECALL_TOP_K_MIN,
            max_k=settings.RECALL_TOP_K_MAX,
            min_score=settings.RECALL_COUNT_MIN_SCORE,
            drop_ratio=settings.RECALL_DROP_RATIO,
            gap_ratio=settings.RECALL_GAP_RATIO,
        )
        by_id = {c.event_id: c for c in scored}
        materials = [_material(meta[eid], by_id[eid]) for eid in decision.selected]

        # 10) 召回计数回写（A13 / A19）
        # 只有进了返回集 **且** 分数达标才算「被用过」——
        # 低分条目即使返回也不能因此获得留存豁免。
        if counter is not None:
            try:
                counter.record_many(
                    namespace,
                    [(m["event_id"], m["score"]) for m in materials],
                    min_score=settings.RECALL_COUNT_MIN_SCORE,
                    now=moment,
                )
            except Exception as exc:  # noqa: BLE001 - 计数是派生数据，不影响召回
                logger.warning("召回计数回写失败（已忽略）: %s", exc)

        return _with_trace(
            RecallResult(
                materials=materials,
                degraded=degraded,
                partial=partial,
                trace={
                    "empty_reason": None,
                    "top_k_decision": decision.to_trace(),
                    "channels": {k: len(v) for k, v in channels.items()},
                    "errors": errors,
                    "date_range": _range_dict(date_range),
                    "gate": gate_trace,
                    **rerank_result.to_trace(),
                },
            ),
            trace_store=trace_store, namespace=namespace, query=query,
        )
    finally:
        if owns_store:
            active_store.close()


# ---------------- 辅助 ----------------


def _with_trace(
    result: RecallResult,
    *,
    trace_store: Any | None,
    namespace: str,
    query: str,
) -> RecallResult:
    """记一次返回快照并附上 `trace_id`。

    空结果**也要记** —— 否则 LLM 无法上报「这次一条都没用」，
    而「召回了却没用」恰恰是有价值的负反馈。

    落库失败只 warn：trace 是辅助数据，不能因为它挂掉就毁掉整次召回。
    """
    if trace_store is None:
        return result
    ids = [
        m.get("event_id")
        for m in result.materials
        if isinstance(m, dict) and m.get("event_id")
    ]
    try:
        result.trace_id = trace_store.record_trace(
            namespace=namespace, query=query, material_ids=ids
        )
    except Exception as exc:  # noqa: BLE001 - 凭据是派生数据
        logger.warning("召回 trace 落库失败（已忽略）: %s", exc)
    return result


def _default_embedder(text: str, settings: Settings):
    from memory.services.embedding import embed_one

    return embed_one(text, settings)


def _clamp01(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, num))


def _days_since(date_start: Any, today: date) -> float:
    """date_start 距今多少天；解析失败按 0 处理（不额外惩罚）。"""
    if not date_start:
        return 0.0
    text = str(date_start)[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return 0.0
    return max(0.0, (today - parsed).days)


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 字 1 token，英文 4 字符 1 token。

    P0 不引入 tokenizer（会拖进 transformers 依赖），
    估得偏保守即可——预算的作用是防止爆窗，不是精确计费。
    """
    if not text:
        return 0
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - chinese
    return chinese + max(1, other // 4)


def _range_dict(r: DateRange) -> dict[str, Any]:
    return {
        "start": r.start.isoformat(),
        "end": r.end.isoformat() if r.end else None,
        "confidence": r.confidence,
        "matched": r.matched,
    }


def _material(hit: dict[str, Any], cand: Candidate) -> dict[str, Any]:
    return {
        "event_id": cand.event_id,
        "summary": hit.get("summary"),
        "date_start": hit.get("date_start"),
        "importance": hit.get("importance"),
        "pinned": hit.get("pinned"),
        "unresolved": hit.get("unresolved") or [],
        "score": round(cand.score, 6),
        "tokens": cand.tokens,
        "forced": cand.forced,
    }
