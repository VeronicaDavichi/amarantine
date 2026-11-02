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
from memory.services.embedding import EmbeddingError, embed_one
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
        return payload


def remember_event(
    *,
    namespace: str,
    summary: str,
    date_hint: str | None = None,
    pinned: bool = False,
    important: bool = False,
    emotion_intensity: float = 0.0,
    context: str | None = None,
    settings: Settings,
    store: WeaviateStore | None = None,
    counter: DirectiveCounter | None = None,
    embedder: object | None = None,
    graph_writer: object | None = None,
    llm_labeler: object | None = None,
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
        settings: 配置。
        store: 可注入的 Weaviate 封装（测试用）。
        counter: 可注入的护栏计数器（测试用）。
        embedder: 可替换的编码函数，签名 ``(text, settings) -> list[float]``。
        graph_writer: 可替换的图写入函数，签名同 `write_event_node`。
        llm_labeler: 可选的 LLM 打标器。
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

    # 2) 时间解析（规则层）
    date_range = resolve_date_hint(date_hint, today=base_day)

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

    card = EventCard(
        namespace=namespace,
        event_id=event_id,
        date_start=date_range.start,
        date_end=date_range.end,
        types=labels.types,
        entities=labels.entities,
        actions=list(getattr(labels, "actions", []) or []),
        summary=summary,
        retention="normal",
        ingested_at=moment,
        importance=importance,
        emotion_intensity=decision.emotion_intensity if emotion_intensity else None,
        pinned=decision.pinned,
        pinned_source=decision.pinned_source,
        user_directive=decision.user_directive,
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
            # 图上的关系**无论如何都追加**：重新提起本身就是一次新提及，
            # 时间线要留下痕迹（关系只追加不覆盖）。
            writer = graph_writer or write_event_node
            outbox = writer(  # type: ignore[operator]
                card,
                settings=settings,
                weaviate_id=existing.get("id"),  # type: ignore[arg-type]
                context=context,
            )
            return RememberResult(
                event_id=event_id,
                created=False,
                revived=was_deleted,
                weaviate_id=existing.get("id"),  # type: ignore[arg-type]
                graph_ok=bool(getattr(outbox, "ok", False)),
            )

        # 8) embed（只对确实要写的内容付费）
        encode = embedder or (lambda text, cfg: embed_one(text, cfg))  # type: ignore[operator]
        vector = encode(summary, settings)  # type: ignore[operator]
        if not isinstance(vector, list):
            raise EmbeddingError(f"编码函数返回了非向量: {type(vector)}")

        # 9) 写事实源
        weaviate_id = active_store.insert_event(card, vector, anomaly=decision.anomaly)

        # 10) 写入成功才记账（否则失败也会消耗配额）
        if decision.pinned:
            counter.record_pinned(namespace, now=moment)

        # 11) 派生图（失败只告警）
        writer = graph_writer or write_event_node
        outbox = writer(  # type: ignore[operator]
            card,
            settings=settings,
            weaviate_id=weaviate_id,
            context=context,
        )

        return RememberResult(
            event_id=event_id,
            created=True,
            anomaly=decision.anomaly,
            weaviate_id=weaviate_id,
            graph_ok=bool(getattr(outbox, "ok", False)),
        )
    finally:
        if owns_store:
            active_store.close()


def _db_path(settings: Settings):
    from memory.services.directive_counter import db_path_from_settings

    return db_path_from_settings(settings)
