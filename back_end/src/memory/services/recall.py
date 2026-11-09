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
import time
from concurrent.futures import ThreadPoolExecutor, wait
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
from memory.core.chunker import keep_top_n
from memory.services.chunk_store import ChunkStore
from memory.services.rerank import rerank
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

#: chunk collection 的检索条数。
#: **与 `RERANK_CANDIDATES` 无关**：那个是「送进 rerank 的候选上限」（决定成本），
#: 这个是「为聚合留多少余量」（决定召回广度）。两者碰巧都叫「候选数」，不要合并。
CHUNK_SEARCH_LIMIT = 100

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
    #: 本次召回总耗时（毫秒）。**单调时钟测量**，不受系统时钟调整影响。
    #: 所有返回路径都有值，包括「空结果」和「partial」。
    elapsed_ms: float = 0.0
    #: 各通道耗时（毫秒），从扇出开始算到该通道返回。
    #: 这些值是**并行**的，相加没有意义 —— 总耗时由最慢的通道决定。
    channel_ms: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "materials": self.materials,
            "degraded": self.degraded,
            "partial": self.partial,
            "trace": self.trace,
            "trace_id": self.trace_id,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "channel_ms": {k: round(v, 1) for k, v in self.channel_ms.items()},
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

    # 单调时钟：测量时长必须用它，`time.time()` 会被系统时钟调整影响。
    # 取在函数最开头 —— 抛 `RecallUnavailableError` 时也要能在 finally 里算到耗时。
    t0 = time.monotonic()
    channel_ms: dict[str, float] = {}

    moment = now or datetime.now(timezone.utc)
    base_day = today or moment.date()
    budget = budget_ms if budget_ms is not None else settings.RECALL_BUDGET_MS

    # 1) 时间意图（规则层；解析不出就给全库）
    date_range: DateRange = resolve_from_query(query, today=base_day)
    # ADR C.B9：`confidence < TIME_CONF_MIN` → **不设 date filter**（退回全库），
    # 标 `time_unresolved=true`。
    #
    # 为什么「低置信宁可不设」而不是「按低置信设」：时间过滤器是硬 AND，
    # 窗口设窄了，窗口外的正确材料就永远回不来（后面所有通道都在窗口内）。
    # 全库搜至少还能靠语义命中。
    #
    # `date_range.end is None` 不算 unresolved —— 那是「单日/持续中」的正常形态，
    # 不是解析失败。
    #
    # `TIME_CONF_MIN <= 0` = 关闭这道闸门（沿用 A10/A25 的「0 = 关信号」约定），
    # 否则 0 会被误读成「全部不通过」。
    date_from = None
    time_unresolved = False
    time_threshold = settings.TIME_CONF_MIN
    if date_range.end is not None:
        if time_threshold <= 0 or date_range.confidence >= time_threshold:
            date_from = f"{date_range.start.isoformat()}T00:00:00Z"
        else:
            time_unresolved = True

    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    candidates_limit = settings.RECALL_CANDIDATES

    def _finish(result: RecallResult) -> RecallResult:
        """填耗时并落 trace —— 四个 return 路径全部经过这里。

        集中在一处而不是逐个 return 填，是因为有 4 个返回点
        （全超时 / 无候选 / 空结果 / 正常），漏一个那条路径的
        `elapsed_ms` 就永远是 0，而 0 看起来像「很快」而不是「没测」。
        """
        result.elapsed_ms = (time.monotonic() - t0) * 1000
        result.channel_ms = dict(channel_ms)
        return _with_trace(result, trace_store=trace_store, namespace=namespace, query=query)

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

        # `VECTOR_TIMEOUT_MS` 是 ADR 给向量通道定的超时（默认 800ms）。
        # 它对齐的是 ADR C.B12 里 800ms 的**优化目标**，而 `COLD_QUERY_P95_MS`
        # 现在是 3000ms 的 SLA 护栏 —— 两者已不相等，也不该相等。
        # 不接的话实际超时是 client 的 20 秒，「召回预算 1500ms」就成了纸面数字。
        #
        # `<= 0` 表示不限（沿用项目「0 = 关信号」的约定），传 None 走 client 默认；
        # 传 0 的话 httpx 会当成「立即超时」，语义完全相反。
        _vec_timeout = (
            settings.VECTOR_TIMEOUT_MS / 1000.0
            if settings.VECTOR_TIMEOUT_MS > 0
            else None
        )

        # 每个通道在**自己的线程里**计时。
        #
        # 不能在 `for name, future in futures.items()` 里记 —— `wait()` 返回时
        # 所有已完成的 future 都完成了，遍历又是顺序的，三个通道会拿到同一个值
        # （实测三个都 P50=25.5 / P95=271.3）。那条埋点测的是「遍历顺序」。
        def _timed(name, fn):
            def wrapper():
                started = time.monotonic()
                try:
                    return fn()
                finally:
                    channel_ms[name] = (time.monotonic() - started) * 1000

            return wrapper

        def run_vector():
            return active_store.search_near_vector(
                query_vec,
                namespace,
                limit=candidates_limit,
                date_from=date_from,
                timeout_s=_vec_timeout,
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
        partial = False
        # 三个通道**并行**跑，所以「等最多 budget」应该用并行原语表达。
        # 旧写法是「顺序 future.result(全预算)」，最坏 3 × budget（实测 3791ms / budget 1500ms），
        # 而且 only 第一个撞线的通道会被标 partial（后面的「借」到时间就不标了）。
        #
        # **不能用 `with ThreadPoolExecutor(...)`**：它的 __exit__ 是 shutdown(wait=True)，
        # 会把没跑完的线程等到结束 —— 那样 wait 只等 1.5s 也白搭，总时长仍是最慢通道的时间。
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            futures = {
                "vector": pool.submit(_timed("vector", run_vector)),
                "bm25": pool.submit(_timed("bm25", run_bm25)),
                "graph": pool.submit(_timed("graph", run_graph)),
            }
            done, not_done = wait(futures.values(), timeout=budget / 1000.0)

            for name, future in futures.items():
                if future in not_done:
                    partial = True
                    errors[name] = "budget exhausted"
                    future.cancel()
                    continue
                try:
                    channel_results[name] = future.result()
                except WeaviateStoreError as exc:
                    # Weaviate 是事实源：挂了必须 503，不能当空结果
                    if name in ("vector", "bm25"):
                        raise RecallUnavailableError(f"{name} 通道失败: {exc}") from exc
                    errors[name] = str(exc)
                except Exception as exc:  # noqa: BLE001
                    errors[name] = f"{type(exc).__name__}: {exc}"
                    logger.warning("通道 %s 失败: %s", name, exc)
        finally:
            # 不等未完成的通道：它们各自的 HTTP 超时兜底
            # （向量通道 800ms 由 spec 017 接管；BM25 仍是 client 默认 20s，见 spec 019 非目标）
            pool.shutdown(wait=False, cancel_futures=True)

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

        # 5.5) A16 chunk 通道（默认关）
        #
        # 长卡会被切成多块各存一个向量。这里查 chunk、每卡保留 N 个高分块，
        # 再把它们的**父 event_id** 并进 vector 通道。
        #
        # 为什么并进 vector 通道而不是新开一个：chunk 命中本质上就是向量相似，
        # 单开一路会引入新的 RRF 权重需要调参。并进 vector 只让「长卡更容易被
        # 召回」，不改变已有排序语义。
        #
        # **失败降级**：chunk 层挂了不影响正常召回（它是召回率的增强，不是必需）。
        chunk_trace: dict[str, Any] = {"chunk_enabled": False}
        if settings.CHUNK_ENABLED:
            chunk_trace = {"chunk_enabled": True, "chunk_hits": 0, "chunk_parents": 0}
            try:
                qvec = encode(query, settings)
                cstore = ChunkStore(settings)
                try:
                    chunk_hits = cstore.search_near_vector(
                qvec, namespace, limit=CHUNK_SEARCH_LIMIT
            )
                finally:
                    cstore.close()
                chunk_trace["chunk_hits"] = len(chunk_hits)
                kept_chunks = keep_top_n(
                    [(h["event_id"], h["score"]) for h in chunk_hits],
                    n=settings.CHUNK_KEEP_PER_EVENT,
                )
                chunk_trace["chunk_parents"] = len(kept_chunks)
                if kept_chunks:
                    merged = list(channels.get("vector", []))
                    for parent, _score in kept_chunks:
                        if parent and parent not in merged:
                            merged.append(parent)
                    channels["vector"] = merged
            except Exception as exc:  # noqa: BLE001
                logger.warning("chunk 召回失败（已降级）: %s", exc)
                chunk_trace["chunk_error"] = type(exc).__name__

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
            # 三个通道一个都没在预算内返回 = 「没等到」，不是「没候选」。
            #
            # 为什么 degraded=True 而不只是 partial：partial 只说「有通道没回来」，
            # 但这次是**全部**没回来，结果完全不可信。也不能抛 503 ——
            # 超时不等于服务挂（真挂已经在上面的 WeaviateStoreError 里 503 了）。
            all_timeout = partial and not channel_results
            return _finish(
                RecallResult(
                    materials=[],
                    degraded=True if all_timeout else degraded,
                    partial=partial,
                    trace={
                        "empty_reason": (
                            "budget_exhausted"
                            if all_timeout
                            else "below_threshold"
                            if gate_filtered
                            else "no_candidate"
                        ),
                        "channels": {},
                        "errors": errors,
                        "date_range": _range_dict(date_range),
                        "gate": gate_trace,
                        # 可解释性：排查「为什么召回不到」时要能区分
                        # 「没解析出时间」和「解析出来了但我不信」
                        "time_unresolved": time_unresolved,
                        "time_confidence": date_range.confidence,
                    },
                ),
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

        # 6.5) 图通道回填元数据（spec 038 A5）
        #
        # 图通道只返回 event_id，没有摘要/时间/重要性。原来这里直接把
        # 「不在 meta 里」的 id 丢掉（第 7 步的过滤），于是**图通道单独命中的卡
        # 永远召不回来** —— 别名展开（路 7）就是典型受害者：
        # 查「王泳」时「王老师」那张卡只可能从图里来，一丢就没了。
        #
        # 回填上限 `GRAPH_META_LIMIT`：图通道最多 50 个 id，全量拉一次
        # 也只是几十次本地 REST，但没必要 —— 排在前面的才是要用的。
        graph_ids = list(channels.get("graph") or [])
        missing = [eid for eid in graph_ids if eid and eid not in meta]
        backfilled = 0
        if missing:
            try:
                for eid in missing[:GRAPH_META_LIMIT]:
                    props = active_store.get_by_event_id(namespace, eid)
                    if not props:
                        continue
                    meta[eid] = {"event_id": eid, **props}
                    backfilled += 1
            except Exception as exc:  # noqa: BLE001 - 回填失败只降级
                logger.warning("图通道元数据回填失败（已降级）: %s", exc)

        # 7) 打分：RRF 归一 → 时效 × 重要性
        ranked = [(eid, s) for eid, s in ranked if eid in meta]
        if not ranked:
            return _finish(
                RecallResult(
                    materials=[],
                    degraded=degraded,
                    partial=partial,
                    trace={
                        "empty_reason": "no_candidate",
                    "time_unresolved": time_unresolved,
                    "time_confidence": date_range.confidence,
                        "channels": {k: len(v) for k, v in channels.items()},
                        "errors": errors,
                        "note": "图通道命中的 id 没有元数据（P0 限制）",
                    },
                ),
            )

        # exact 别名命中的卡：**强制保留**（spec 038 REQ-2 / A5）
        #
        # 别名是用户亲口说的等价关系（`称呼王泳为王老师`），不是模型猜的。
        # 这类卡常常只从图通道来（摘要里没有查询词），RRF 分天然低，
        # 会被 `MIN_SCORE` 当「不相关」丢掉 —— 但用户查「王泳」时，
        # 「王老师」名下的卡正是他要的。按 D7 的硬信号口径，它们是
        # `forced`，允许突破条数上限（overflow 会如实上报）。
        exact_alias_ids: set[str] = set()
        if graph_hits is not None:
            for hit in getattr(graph_hits, "alias_hits", ()) or ():
                if getattr(hit, "label", "") == "exact":
                    # 别名两侧的名字都可能出现在事件边里，保守起见把两侧都算上
                    exact_alias_ids.add(getattr(hit, "canonical", ""))
                    exact_alias_ids.add(getattr(hit, "alias", ""))
        # 把「名字」换成「事件 id」：图通道已经返回了这些卡
        exact_alias_event_ids: set[str] = set()
        if exact_alias_ids:
            for eid in channels.get("graph", []):
                props = meta.get(eid) or {}
                names = {
                    str(props.get("summary") or ""),
                }
                if any(n and any(a and a in n for a in exact_alias_ids) for n in names):
                    exact_alias_event_ids.add(eid)
            # 退一步：图通道给的 id 本就来自别名展开，无法逐条归因时全部保留
            if not exact_alias_event_ids:
                exact_alias_event_ids = set(channels.get("graph", []))

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
            forced = (
                bool(item.get("pinned"))
                or bool(item.get("unresolved"))
                or eid in exact_alias_event_ids
            )
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
            return _finish(
                RecallResult(
                    materials=[],
                    degraded=degraded,
                    partial=partial,
                    trace={
                        "empty_reason": "below_threshold",
                    "time_unresolved": time_unresolved,
                    "time_confidence": date_range.confidence,
                        "top_score": round(top, 4),
                        "threshold": settings.RECALL_EMPTY_THRESHOLD,
                        "channels": {k: len(v) for k, v in channels.items()},
                        "errors": errors,
                    },
                ),
            )

        # 8.5) rerank 精排（A27）
        #
        # **位置**：RRF 融合之后、动态 K 截断之前 —— 截断后再 rerank 没有意义，
        # 候选已经丢了。
        #
        # **失败不抛**：rerank 是增强项，挂了就退回 RRF 顺序继续，整体仍是 200。
        # 这是 ADR A27 的核心断言（kill 服务后材料集非空 + trace 标 skipped）。
        # `RERANK_CANDIDATES` 真的在这里生效：rerank 是 CPU 上的昂贵操作，
        # 候选越多越慢（实测每批 ~0.3s）。截断后再送，不是只写个配置放着。
        rerank_pool = scored[: settings.RERANK_CANDIDATES]
        rerank_result = rerank(
            query,
            [
                (c.event_id, str(meta.get(c.event_id, {}).get("summary") or ""))
                for c in rerank_pool
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
            # **rerank 分的尺度**上判断，不再是 RRF 分。两套分数不能混用于
            # 同一个排序，只能整套换掉。
            #
            # **必须归一化到「最高分 = 1.0」**：TEI 的 rerank 分是 sigmoid 后
            # 的概率，实测同一批候选只有 0.057 / 0.048 / 0.00002 这个量级，
            # 而 `RECALL_COUNT_MIN_SCORE` 是 0.5 —— 直接替换会让 `decide_k`
            # 第 2 步「绝对过滤」把材料全砍光，召回变空。
            #
            # 归一化不是凑数：rerank 分是 **per-query 的相对信号**，
            # 不同 query 的 logit 分布没有可比性，绝对值本就不该直接比阈值。
            # 归一化后 min_score=0.5 的语义变成「保留达到最高分 50% 的候选」，
            # 这正是「相对相关度」的本意。
            peak = max(rerank_result.scores.values())
            norm = {
                eid: (score / peak if peak > 0 else 0.0)
                for eid, score in rerank_result.scores.items()
            }
            scored = [
                replace(c, score=norm[c.event_id]) if c.event_id in norm else c
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

        return _finish(
            RecallResult(
                materials=materials,
                degraded=degraded,
                partial=partial,
                trace={
                    "empty_reason": None,
                    "time_unresolved": time_unresolved,
                    "time_confidence": date_range.confidence,
                    "top_k_decision": decision.to_trace(),
                    "channels": {k: len(v) for k, v in channels.items()},
                    "errors": errors,
                    "date_range": _range_dict(date_range),
                    "gate": gate_trace,
                    **chunk_trace,
                    **rerank_result.to_trace(),
                },
            ),
        )
    finally:
        # 超目标告警放 finally 而不是 return 前：慢到超时抛异常的查询**也是慢查询**，
        # 更需要被看见。这里独立再算一次总耗时（与 _finish 的差一个 trace 写入，可忽略）。
        _total_ms = (time.monotonic() - t0) * 1000
        _limit = settings.COLD_QUERY_P95_MS
        # `<= 0` = 关闭告警（沿用项目「0 = 关信号」约定）。
        # 不这么写的话 0 会变成「0ms 目标、每次查询都告警」，把日志淹掉。
        if _limit > 0 and _total_ms > _limit:
            logger.warning(
                "冷查询耗时超目标 | %.0fms > %dms | 通道 %s",
                _total_ms,
                _limit,
                {k: round(v) for k, v in channel_ms.items()} or "(无)",
            )
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
