"""B1/B6 `memory_remember` 编排。

一条写入的完整链路（顺序即代码顺序）：

    校验参数 → 解析时间 → 应用护栏 → 打标 → 算 importance
    → 生成 event_id → 幂等检查 → embed → 写 Weaviate → 记护栏计数 → 写图

几个刻意的设计：

- **幂等检查放在 embed 之前**：重复内容不该浪费一次 embedding 调用。
- **护栏计数放在写入成功之后**：写失败还记账，会让正常用户被误降级。
- **图失败不影响返回值**：ADR D2 规定图可丢弃重建，只留告警。
- **重要度计算与写卡分离**：`merge_importance` 在 guards 里，这里只做组装。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from memory.config import Settings
from memory.core.event_id import make_event_id
from memory.core.guards import (
    GuardError,
    apply_directive_guards,
    merge_importance,
    validate_remember_args,
)
from memory.core.time_resolver import resolve_date_hint
from memory.models.event_card import EventCard
from memory.services.directive_counter import DirectiveCounter
from memory.core.chunker import split_text
from memory.services.chunk_store import ChunkStore
from memory.services.embedding import EmbeddingError, embed_one, embed_texts
from memory.services.embedding_guard import EmbeddingIndexMeta
from memory.services.vocab_version import current_version
from memory.services.graph_outbox import write_event_node
from memory.services.labeler import label_by_rules
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger("memory.remember")

#: 未给出任何重要性信号时的基础分（B6 的四档 rubrics 在 P1 细化）
BASE_IMPORTANCE = 0.5


def _run_dual_check(
    *,
    namespace: str,
    summary: str,
    rules: object,
    date_range: object,
    settings: Settings,
    llm_labeler: object,
) -> None:
    """B5 双跑：规则层与 LLM 层对同一句话各打一次标，分歧进复核队列。

    **只观察，不改写卡内容**。写卡用的仍是 `rules`，理由见调用处注释：
    `types` 参与 `event_id`，让 LLM 改写会让幂等失效。

    整个函数**吞掉所有异常**：复核队列是旁路诊断，
    它挂了不该让「记住这件事」失败。LLM 超时同理。
    """
    try:
        from memory.services.label_review import queue_from_settings
        from memory.services.llm_labeler import compare_labels

        rule_types = list(getattr(rules, "types", []) or [])
        refined = llm_labeler(summary, rule_types)  # type: ignore[operator]
        if refined is None or not getattr(refined, "types", None):
            logger.debug("双跑：LLM 无结果，跳过复核")
            return

        cmp = compare_labels(
            rules,  # type: ignore[arg-type]
            refined,  # type: ignore[arg-type]
            agreement_min=settings.LABEL_DUAL_RUN_JACCARD,
        )
        if not cmp.needs_review:
            logger.debug("双跑一致 | agreement=%.2f | %s", cmp.agreement, summary[:24])
            return

        # event_id 由规则层 types 决定（与写入完全同参），保证能对上那张卡
        event_id = make_event_id(
            namespace=namespace,
            date_start=date_range.start,  # type: ignore[attr-defined]
            date_end=getattr(date_range, "end", None),
            types=rule_types,
            entity_names=[e.name for e in getattr(rules, "entities", [])],
            summary=summary,
        )
        queue_from_settings(settings).push(
            namespace=namespace,
            event_id=event_id,
            summary=summary,
            rules_types=rule_types,
            llm_types=list(refined.types),
            agreement=cmp.agreement,
        )
        logger.info(
            "双跑分歧入队 | agreement=%.2f | rules=%s llm=%s | %s",
            cmp.agreement,
            rule_types,
            list(refined.types),
            summary[:24],
        )
    except Exception as exc:  # noqa: BLE001 - 旁路诊断，绝不影响写入
        logger.warning("双跑复核失败（已忽略）: %s", exc)


@dataclass(frozen=True)
class RememberResult:
    """工具层要返回给 LLM 的结构。"""

    event_id: str
    created: bool
    anomaly: str | None = None
    weaviate_id: str | None = None
    graph_ok: bool = True
    #: 图没写成时，是否已经记进意图日志（攒着等补跑）
    graph_pending: bool = False
    #: 这条内容曾被告知「忘掉」，这次因为重新提起而被恢复
    revived: bool = False

    def to_tool_result(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "created": self.created,
        }
        if self.anomaly:
            payload["anomaly"] = self.anomaly
        if self.revived:
            payload["revived"] = True
        # **图没写成必须让调用方看见。**
        # 记忆进了 Weaviate（向量通道还能召回），但图关系缺失
        # （图通道永远找不到）—— 这是「只进了一半」。
        # 原来 `graph_ok` 这个字段全代码库没人读，等于静默降级。
        if not self.graph_ok:
            payload["graph_pending"] = self.graph_pending
            payload["degraded"] = "graph_write_failed"
        return payload


def remember_event(
    *,
    namespace: str,
    summary: str,
    date_hint: str | None = None,
    #: 消息发送时间（spec 040）。相对时间（「昨天」）以它为基准换算
    msg_time: datetime | None = None,
    #: 这张卡属于哪个会话（spec 046）
    session_id: str | None = None,
    #: 这张卡解决了哪些旧卡的含糊（spec 046 归并）
    resolves: list[str] | None = None,
    pinned: bool = False,
    important: bool = False,
    emotion_intensity: float = 0.0,
    context: str | None = None,
    raw_quote_ids: list[str] | None = None,
    settings: Settings,
    store: WeaviateStore | None = None,
    counter: DirectiveCounter | None = None,
    embedder: object | None = None,
    graph_writer: object | None = None,
    llm_labeler: object | None = None,
    intent_store: object | None = None,
    meta: EmbeddingIndexMeta | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> RememberResult:
    """写入一条记忆。

    Args:
        namespace: 记忆域（由入口层从环境变量取得，**不接受工具参数**）。
        summary: 事件摘要（LLM 给定边界即作用域）。
        date_hint: 自然语言时间线索。
        pinned: 用户是否显式要求记住。
        important: 用户是否强调重要。
        emotion_intensity: 情绪强度 0–1。
        context: 当前对话话题（如 ``"A股"``）。给了才在图上建 Context 边。
        raw_quote_ids: 这条摘要出自哪些原始消息（`ingest` 传入）。
            **不进 `event_id` 哈希**，所以同一摘要重复导入仍幂等。
        settings: 配置。
        store: 可注入的 Weaviate 封装（测试用）。
        counter: 可注入的护栏计数器（测试用）。
        embedder: 可替换的编码函数，签名 ``(text, settings) -> list[float]``。
        graph_writer: 可替换的图写入函数，签名同 `write_event_node`。
        llm_labeler: 可选的 LLM 打标器。
        intent_store: 可注入的意图日志（测试用）。缺省按 DATABASE_URL 建。
        now: 当前时刻（UTC），缺省取系统时间。
        today: 「今天」的基准日，缺省取 `now` 的日期。

    Raises:
        GuardError: 参数非法（工具层应转 isError）。
        EmbeddingError: embedding 服务不可用（工具层应转 503）。
        WeaviateStoreError: 写入失败（工具层应转 503）。
    """
    # 1) 参数校验（不信任 LLM 的类型自觉）
    validate_remember_args(
        summary=summary,
        pinned=pinned,
        important=important,
        emotion_intensity=emotion_intensity,
    )

    moment = now or datetime.now(timezone.utc)
    base_day = today or moment.date()

    # 1.5) A25：索引模型校验
    #
    # **必须放在 embed 之前**：不一致时白调一次 embedding 服务，既花钱又慢，
    # 而且调用成功会让「写入失败」看起来像「编码失败」，误导排查。
    #
    # 为什么是错误而不是告警：不同模型的向量可能维度相同、空间完全不同，
    # 混写不会报错，只会让相似度静默变错（见 embedding_guard 模块头）。
    meta_store = meta or EmbeddingIndexMeta(_db_path(settings))
    meta_store.ensure_model(namespace, settings.EMBEDDING_MODEL)

    # 1.6) A23：记下打标用的词表版本
    # 半年后回看这张卡的 types 时，需要知道它是按哪版词表打的 —— 否则
    # 词表改过之后，标签就失去了可解释性。版本表在同一个 SQLite 里。
    vocab_version = current_version(settings)

    # 2) 时间解析（规则层）
    # spec 040：相对时间（昨天/上周）必须锚在**消息时间**上，不是导入日。
    # 10-30 发的「昨天开的会」是 10-29，不是「今天往前一天」。
    anchor_day = msg_time.date() if msg_time is not None else base_day
    date_range = resolve_date_hint(date_hint, today=anchor_day)

    # 事件时间来源，按可信度从高到低：
    # stated（原话给日期）> relative（按消息时间换算）> msg_ts（消息时间当事件时间）
    # > imported（都判不出，用导入时间 —— 明确标出来，不假装精确）
    from memory.services.importer import time_source_for

    time_source = time_source_for(date_hint, msg_time)

    # 3) 护栏（需要 24h 计数）
    counter = counter or DirectiveCounter(_db_path(settings))
    used = counter.count_pinned(namespace, now=moment)
    decision = apply_directive_guards(
        pinned=pinned,
        important=important,
        emotion_intensity=emotion_intensity,
        pinned_in_window=used,
        daily_max=settings.USER_DIRECTIVE_DAILY_MAX,
        explicit_important_floor=settings.EXPLICIT_IMPORTANT_FLOOR,
        emotion_boost=settings.EMOTION_BOOST,
    )

    # 4) 打标
    #    **规则层决定写什么，LLM 只做复核参考**（B5）。
    #    原因：types 参与 event_id 哈希（D1）。若让 LLM 改写 types，
    #    同一条事件重写就会得到不同 event_id —— 幂等直接失效，
    #    而幂等是「重复上报不产生第二份」的唯一保障。
    #    所以这里**不**把 llm_labeler 传给 label_event（那会覆盖 types）。
    labels = label_by_rules(summary, vocab=settings.TYPES_VOCAB or None)
    if llm_labeler is not None and settings.LABEL_DUAL_RUN:
        _run_dual_check(
            namespace=namespace,
            summary=summary,
            rules=labels,
            date_range=date_range,
            settings=settings,
            llm_labeler=llm_labeler,
        )

    # 5) 重要度：基础分 → 下限 → 情绪加成（封顶 1.0）
    importance = merge_importance(
        base=BASE_IMPORTANCE,
        floor=decision.importance_floor,
        boost=decision.importance_boost,
    )

    # 6) 内容哈希主键（namespace 参与，防跨域撞 id）
    event_id = make_event_id(
        namespace=namespace,
        date_start=date_range.start,
        date_end=date_range.end,
        types=labels.types,
        entity_names=[e.name for e in labels.entities],
        summary=summary,
    )

    # 6.5) 姓氏声明落库（`这人姓种`）。
    #      内存里已经在 `label_by_rules` 里生效了，这里补持久化。
    #      放在幂等短路**之前**：重报同一条时也要保证声明在库里。
    _record_surname_declarations(
        namespace,
        event_id,
        summary,
        surnames=getattr(labels, "surnames", ()),
        settings=settings,
    )

    card = EventCard(
        namespace=namespace,
        event_id=event_id,
        date_start=date_range.start,
        date_end=date_range.end,
        # spec 040：消息时间与事件时间分开存，来源可追溯（不进 event_id 哈希）
        msg_time=msg_time,
        time_source=time_source,
        session_id=session_id,
        resolves=list(resolves or []),
        types=labels.types,
        entities=labels.entities,
        actions=list(getattr(labels, "actions", []) or []),
        alias_claims=list(getattr(labels, "alias_claims", []) or []),
        summary=summary,
        retention="normal",
        ingested_at=moment,
        importance=importance,        emotion_intensity=decision.emotion_intensity if emotion_intensity else None,
        pinned=decision.pinned,
        pinned_source=decision.pinned_source,
        user_directive=decision.user_directive,
        # 原文溯源（ADR 4.8）。**不参与 `event_id` 哈希** —— 否则同一段摘要
        # 因为挂在不同 quote 上会被当成两个事件，幂等直接失效。
        raw_quote_ids=list(raw_quote_ids or []),
    )
    card.check_invariants()

    # 7) 幂等：同内容已存在则不重复写（A7）
    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    try:
        if store is None:
            active_store.ensure_collection()

        existing = active_store.get_by_event_id(namespace, event_id)
        if existing is not None:
            was_deleted = bool(existing.get("is_deleted"))
            if was_deleted:
                # 用户把「忘掉」的那件事原样又说了一遍 → 复活它。
                # 只看到「卡已存在」就静默返回，会让这条记忆永远停在已忘状态，
                # 用户说什么都没用（除非换措辞让 event_id 变掉）。
                active_store.revive(event_id, namespace)
                logger.info(
                    "已遗忘的记忆被重新提起，复活 | event_id=%s | %s",
                    event_id,
                    summary[:24],
                )
            # 回填内容字段。
            #
            # 幂等短路在 `insert_event` **之前**，所以 store 层那个 422 分支
            # 在正常流程里走不到。schema 升级（比如给事件卡加 `alias_claims`）
            # 之后，老对象不会自己长出新字段 —— 必须在这里显式补一次，
            # 否则「同一条事件重报」永远拿不到新标注。
            #
            # 只补缺失的内容字段，`recall_count` / `is_deleted` 这些运行时状态
            # 由 store 层的白名单挡着，不会被这次写入改写。
            backfilled = 0
            backfill = getattr(active_store, "backfill_event", None)
            if callable(backfill):
                backfilled = backfill(card) or 0
            if backfilled:
                logger.info(
                    "回填老对象缺失字段 %d 个 | event_id=%s", backfilled, event_id
                )
            # 图上的关系**无论如何都追加**：重新提起本身就是一次新提及，
            # 时间线要留下痕迹（关系只追加不覆盖）。
            #
            # **幂等分支也要记账**：重报同一条时图写失败，照样要能补。
            # 漏了这一段的话，「第一次图挂了，再提一次」永远修不好 ——
            # 只会一遍遍返回 created=False 而图始终是空的。
            graph_ok, graph_pending, outbox = _write_graph_with_intent(
                namespace,
                card,
                weaviate_id=existing.get("id"),  # type: ignore[arg-type]
                context=context,
                settings=settings,
                intent_store=intent_store,
                graph_writer=graph_writer,
                now=moment,
            )
            _record_alias_rejects(
                namespace,
                event_id,
                summary,
                alias_rejects=getattr(labels, "alias_rejects", ()),
                outbox=outbox,
                settings=settings,
            )
            return RememberResult(
                event_id=event_id,
                created=False,
                revived=was_deleted,
                weaviate_id=existing.get("id"),  # type: ignore[arg-type]
                graph_ok=graph_ok,
                graph_pending=graph_pending,
            )

        # 8) embed（只对确实要写的内容付费）
        encode = embedder or (lambda text, cfg: embed_one(text, cfg))  # type: ignore[operator]
        vector = encode(summary, settings)  # type: ignore[operator]
        if not isinstance(vector, list):
            raise EmbeddingError(f"编码函数返回了非向量: {type(vector)}")

        # 9) 写事实源
        weaviate_id = active_store.insert_event(
            card,
            vector,
            anomaly=decision.anomaly,
            vocab_version=vocab_version,
        )

        # 9.5) A16 chunk 层（默认关）
        #
        # **失败不影响主流程**：卡已经写成功了，chunk 只是让长摘要能被
        # 分段检索到的增强项。这里吞异常是刻意的 —— 与 rerank 降级同理，
        # 增强项不该成为可用性的单点。
        if settings.CHUNK_ENABLED:
            try:
                chunks = split_text(
                    summary,
                    size=settings.CHUNK_SIZE,
                    overlap=settings.CHUNK_OVERLAP,
                )
                # 只有真会被切开时才写 chunk —— 单块等于原卡向量，重复存没意义
                if len(chunks) > 1:
                    cstore = ChunkStore(settings)
                    try:
                        cvecs = embed_texts([c.text for c in chunks], settings)
                        cstore.insert_chunks(
                            namespace,
                            card.event_id,
                            chunks,
                            cvecs,
                            date_start=card.date_start,
                        )
                    finally:
                        cstore.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("chunk 写入失败（已忽略，卡已写成功）: %s", exc)

        # 10) 写入成功才记账（否则失败也会消耗配额）
        if decision.pinned:
            counter.record_pinned(namespace, now=moment)

        # 11) 派生图：先记账，再动手，成功销账
        #
        # **顺序不能反。**「动手之后、记账之前」崩溃 = 什么都没留下。
        # 先写意图日志，「记账之后、销账之前」崩溃 = 重启时补跑。
        #
        # happy path 一秒没变慢：图写入仍然同步、仍然在返回前完成，
        # 只是前后各多一条 SQLite 写入（INSERT + DELETE）。
        graph_ok, graph_pending, outbox = _write_graph_with_intent(
            namespace,
            card,
            weaviate_id=weaviate_id,
            context=context,
            settings=settings,
            intent_store=intent_store,
            graph_writer=graph_writer,
            now=moment,
        )
        _record_alias_rejects(
            namespace,
            event_id,
            summary,
            alias_rejects=getattr(labels, "alias_rejects", ()),
            outbox=outbox,
            settings=settings,
        )

        return RememberResult(
            event_id=event_id,
            created=True,
            anomaly=decision.anomaly,
            weaviate_id=weaviate_id,
            graph_ok=graph_ok,
            graph_pending=graph_pending,
        )
    finally:
        if owns_store:
            active_store.close()


def _db_path(settings: Settings):
    from memory.services.directive_counter import db_path_from_settings

    return db_path_from_settings(settings)


#: 意图日志按 db 路径缓存（`__init__` 会跑一次建表脚本，不必每次都跑）
_INTENT_STORES: dict[str, object] = {}


def _intent_store(settings: Settings, injected: object | None):
    """拿到意图日志。注入优先，否则按 DATABASE_URL 建（并缓存）。"""
    if injected is not None:
        return injected
    from memory.services.pending_task import (
        PendingTaskStore,
        db_path_from_settings,
    )

    path = str(db_path_from_settings(settings))
    cached = _INTENT_STORES.get(path)
    if cached is None:
        cached = PendingTaskStore(db_path_from_settings(settings))
        _INTENT_STORES[path] = cached
    return cached


def _enqueue_intent(
    namespace: str,
    card: EventCard,
    *,
    weaviate_id: str | None,
    context: str | None,
    settings: Settings,
    intent_store: object | None,
    now: datetime | None = None,
) -> int | None:
    """记一笔「这张卡要写图」。失败返回 None（并打 ERROR）。

    **意图日志挂了不该拦住写入**：事实层（Weaviate）已经落盘，
    这时候抛异常等于把已经成功的写入变成失败，调用方会以为没记住。
    但也不能装作没事 —— 所以是 ERROR 级日志 + `graph_pending=False`。

    `now` 要透传：入队时刻就是 `next_retry_at` 的起点。不传的话
    补跑的时间轴会跟写入用两套时钟（测试里尤其明显：入队按真实时间，
    查询按注入时间，永远差一截）。
    """
    try:
        store = _intent_store(settings, intent_store)
        return store.enqueue(  # type: ignore[union-attr]
            namespace,
            "graph_write",
            card.event_id,
            {
                "card": card.model_dump(mode="json"),
                "weaviate_id": weaviate_id,
                "context": context,
            },
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "意图日志写入失败，这条事件没有补跑兜底 event=%s: %s",
            card.event_id,
            exc,
        )
        return None


def _complete_intent(
    intent_id: int | None, *, settings: Settings, intent_store: object | None
) -> None:
    """图写成功后销账。"""
    if intent_id is None or intent_id < 0:
        return
    try:
        store = _intent_store(settings, intent_store)
        store.complete(intent_id)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        # 销账失败只会导致一次多余的重放，而重放是幂等的 —— 不致命
        logger.warning("意图日志销账失败 task=%s: %s", intent_id, exc)


def _fail_intent(
    intent_id: int | None,
    *,
    error: str,
    settings: Settings,
    intent_store: object | None,
    now: datetime | None = None,
) -> None:
    """图写失败后记一次失败，等补跑。"""
    if intent_id is None or intent_id < 0:
        logger.error("图写入失败且没有意图日志兜底: %s", error)
        return
    try:
        store = _intent_store(settings, intent_store)
        status = store.fail(intent_id, error, now=now)  # type: ignore[union-attr]
        logger.warning(
            "图写入失败 task=%s status=%s: %s", intent_id, status, error
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("意图日志记账失败 task=%s: %s", intent_id, exc)


def _write_graph_with_intent(
    namespace: str,
    card: EventCard,
    *,
    weaviate_id: str | None,
    context: str | None,
    settings: Settings,
    intent_store: object | None,
    graph_writer: object | None,
    now: datetime | None = None,
) -> tuple[bool, bool, object]:
    """写图并记账。返回 `(graph_ok, graph_pending, outbox)`。

    两条写入路径（新建 / 幂等重报）都走这里 —— 记账逻辑只有一份，
    不会出现「主路径有兜底、幂等路径没有」这种不对称。

    `graph_pending` 的含义是**有欠账、且已经记在盘上等人补**
    （`graph_ok=False` 且入队成功），不是「这次入过队」。
    写成功时它是 False —— 成功的写入没有欠账。

    `outbox` 原样带出去，调用方要用它的 `alias_skipped`
    （哪些别名边被判据拒了）落 `alias_reject` 表。

    写图抛异常时**先把账记成失败再往上抛**：行为跟以前一致
    （D2：图挂了不该影响主链路，异常由 `write_event_node` 内部消化成
    `ok=False`），但账不会漏。
    """
    intent_id = _enqueue_intent(
        namespace,
        card,
        weaviate_id=weaviate_id,
        context=context,
        settings=settings,
        intent_store=intent_store,
        now=now,
    )
    pending = intent_id is not None and intent_id >= 0

    writer = graph_writer or write_event_node
    try:
        outbox = writer(  # type: ignore[operator]
            card,
            settings=settings,
            weaviate_id=weaviate_id,
            context=context,
        )
    except Exception as exc:
        _fail_intent(
            intent_id,
            error=f"{type(exc).__name__}: {exc}",
            settings=settings,
            intent_store=intent_store,
            now=now,
        )
        raise

    graph_ok = bool(getattr(outbox, "ok", False))
    if graph_ok:
        _complete_intent(intent_id, settings=settings, intent_store=intent_store)
    else:
        _fail_intent(
            intent_id,
            error=getattr(outbox, "error", None) or "graph write returned ok=False",
            settings=settings,
            intent_store=intent_store,
            now=now,
        )
    return graph_ok, (pending and not graph_ok), outbox


def _record_surname_declarations(
    namespace: str,
    event_id: str,
    summary: str,
    *,
    surnames: object,
    settings: Settings,
) -> int:
    """把「这人姓种」这类声明落库。返回新登记了几个。

    内存里已经生效了（`label_by_rules` 先登记再抽别名，同一句里的别名
    才能立刻受益）。这里补的是**持久化**：重启后靠它灌回来。

    失败只告警 —— 姓氏是判据的辅助信息，不能因为它拖垮记忆写入。
    """
    chars = [c for c in (surnames or ()) if isinstance(c, str)]  # type: ignore[union-attr]
    if not chars:
        return 0
    from memory.services.surname_decl import registry_from_settings

    try:
        registry = registry_from_settings(settings)
        fresh = registry.declare(
            namespace,
            chars,
            evidence=summary[:200],
            event_id=event_id,
        )
    except Exception as exc:  # noqa: BLE001 - 旁路，绝不影响写入
        logger.warning("姓氏声明落库失败（已忽略）: %s", exc)
        return 0
    if fresh:
        logger.info(
            "登记姓氏 %s | namespace=%s | event_id=%s",
            "".join(chars),
            namespace,
            event_id,
        )
    return fresh


def _record_alias_rejects(
    namespace: str,
    event_id: str,
    summary: str,
    *,
    alias_rejects: object,
    outbox: object,
    settings: Settings,
) -> int:
    """把「别名声明被拒」的痕迹落到 `alias_reject` 表。返回记了几条。

    ## 为什么两头都要记

    别名声明从原话到图上一条边，有两道判据，**两侧都可能被拒**：

    - **左值**（`三哥主打的`）在 `extract_alias_claims` 就被拒，
      claim 压根不生成 —— LLM 复核层连看的资格都没有。这是最隐形的一种。
    - **右值**（`那个ai中最弱智的`）claim 生成了、留在卡上，
      但建边时被 `_partition_alias_claims` 跳过。

    原来两边都不留痕迹，于是「为什么这条边没建」只能靠猜。
    `印度人`、`科学家`、`种师道` 三次都是这么藏起来的。

    留痕是旁路：它自己挂了绝不能让记忆写不进去（见 `record_safe`）。
    """
    from memory.services.alias_reject import record_safe

    rejects: list[dict[str, str]] = []
    for item in alias_rejects or ():  # type: ignore[union-attr]
        dump = getattr(item, "to_dict", None)
        if callable(dump):
            rejects.append(dump())
    for item in getattr(outbox, "alias_skipped", ()) or ():
        if isinstance(item, dict):
            rejects.append(item)
    if not rejects:
        return 0

    recorded = record_safe(
        settings,
        namespace=namespace,
        event_id=event_id,
        rejects=rejects,
        source_text=summary[:200],
    )
    if recorded:
        logger.info(
            "别名声明被拒 %d 条（已留痕） | event_id=%s | %s",
            recorded,
            event_id,
            summary[:24],
        )
    return recorded


def warm_up_dictionary(namespace: str, *, settings: Settings) -> int:
    """从图上拉已有名字，灌进 jieba 词典。返回灌了多少个。

    ## 为什么是「启动时」而不是「每次写入后」

    自动反哺会破坏幂等性：

        event_id = hash(namespace | 日期 | types | entity_names | summary)

    每写一条就灌一次词，下一次同样的句子分词结果就变了 →
    `entity_names` 变了 → `event_id` 变了 → **一条记忆裂成两条**。
    实测同一句话写两次得到 `evt_aa1e78d3…` 和 `evt_520f8855…`。

    N4 的幂等性是地基，不能为了分词让路。

    启动时灌一次没有这个问题：**同一个进程内词典固定不变**，
    同一句话写两次的切分必然一致。

    ## 代价

    新名字要等下次启动才能切对。可以接受 —— 首次出现本来就会切错，
    第二次起也未必立刻需要，等到重启够了。

    ## 失败不抛

    图连不上就只灌静态词典，不影响服务启动。
    """
    try:
        from memory.services.graph_outbox import list_known_names
        from memory.services.labeler import load_entity_dictionary

        names = list_known_names(namespace, settings=settings)
    except Exception:  # noqa: BLE001 - 图不可用不该拦住启动
        logger.warning("warm-up 拉名字失败，只用静态词典", exc_info=True)
        return 0

    try:
        return load_entity_dictionary(names=names)
    except Exception:  # noqa: BLE001
        logger.warning("warm-up 灌词典失败（已忽略）", exc_info=True)
        return 0
