"""B9 图 outbox：幂等写 Event 节点，失败只告警。

ADR D2 的红线是「**图可丢弃重建**」：

- 写入顺序：先 Weaviate 成功 → 再发图更新
- 图写失败**只告警，不回滚、不阻断**
- 节点/边统一带 `namespace`，禁止跨域

所以本模块的契约很明确：**它永远不抛异常**（除非参数本身非法），
只返回结果对象并把失败原因带出来，由调用方记日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from memory.config import Settings
from memory.models.event_card import EventCard

logger = logging.getLogger("memory.graph")

#: Event 节点的 MERGE 键（namespace + event_id）
MERGE_CYPHER = """
MERGE (e:Event {namespace: $namespace, event_id: $event_id})
SET e.summary        = $summary,
    e.date_start     = $date_start,
    e.date_end       = $date_end,
    e.types          = $types,
    e.importance     = $importance,
    e.pinned         = $pinned,
    e.retention      = $retention,
    e.weaviate_id    = $weaviate_id,
    e.ingested_at    = $ingested_at
RETURN e
"""


@dataclass(frozen=True)
class OutboxResult:
    """图更新的结果。`ok=False` 时链路继续走，只是留一条告警。"""

    ok: bool
    error: str | None = None


def write_event_node(
    card: EventCard,
    *,
    settings: Settings,
    weaviate_id: str | None,
    driver: object | None = None,
) -> OutboxResult:
    """把事件卡同步成图里的 `Event` 节点。

    Args:
        card: 事件卡。
        settings: Neo4j 连接配置。
        weaviate_id: 回指 Weaviate 对象的 UUID（A8 要求双向可解析）。
        driver: 可注入的 neo4j driver（测试用）。

    Returns:
        OutboxResult；**不会抛异常**，失败仅告警（ADR D2）。
    """
    owns_driver = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )

        params = {
            "namespace": card.namespace,
            "event_id": card.event_id,
            "summary": card.summary,
            "date_start": card.date_start.isoformat(),
            "date_end": card.date_end.isoformat() if card.date_end else None,
            "types": list(card.types),
            "importance": card.importance,
            "pinned": card.pinned,
            "retention": card.retention,
            "weaviate_id": weaviate_id,
            "ingested_at": card.ingested_at.isoformat(),
        }
        with drv.session() as session:
            session.run(MERGE_CYPHER, **params)
        return OutboxResult(ok=True)
    except Exception as exc:  # noqa: BLE001 - 图失败绝不阻断主链路
        message = f"图节点写入失败（已忽略，等待重建对账）: {exc}"
        logger.warning("%s event_id=%s", message, card.event_id)
        return OutboxResult(ok=False, error=str(exc))
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001 - 关闭失败也不该影响主流程
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)
