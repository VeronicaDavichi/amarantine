"""C1 Neo4j 实体扩展通道。

ADR 的超时矩阵里，图通道是 **200ms 且失败直接丢弃该路**：

- 图可丢弃重建（D2），所以它天然是可降级的
- 但它不能拖慢整体：`degraded=true` 只是标记某路缺失，不影响返回

P0 的图查询刻意保持简单：按实体名在 `summary` 里做包含匹配。
真正的多跳实体扩展要等图结构补齐后再上——**先用能工作的最简单版本**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from memory.config import Settings

logger = logging.getLogger("memory.graph_search")

#: 图通道超时（ADR D3）
GRAPH_TIMEOUT_S = 0.2

#: 一次查询最多用几个实体名，避免 Cypher 参数爆炸
MAX_ENTITY_NAMES = 8

#: 实体名最短长度——单字实体噪声太大
MIN_ENTITY_LEN = 2

_SEARCH_CYPHER = """
MATCH (e:Event {namespace: $namespace})
WHERE ANY(name IN $names WHERE e.summary CONTAINS name)
RETURN e.event_id AS event_id
LIMIT $limit
"""


@dataclass(frozen=True)
class GraphHits:
    """图通道结果。`ok=False` 时上游应置 `degraded=true` 并继续。"""

    event_ids: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


def _clean_names(entity_names: list[str]) -> list[str]:
    """过滤过长/过短/重复的实体名，保持顺序。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in entity_names:
        text = str(name).strip()
        if len(text) < MIN_ENTITY_LEN or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= MAX_ENTITY_NAMES:
            break
    return cleaned


def search_by_entities(
    entity_names: list[str],
    namespace: str,
    *,
    settings: Settings,
    limit: int = 50,
    timeout_s: float = GRAPH_TIMEOUT_S,
    driver: Any | None = None,
) -> GraphHits:
    """按实体名扩展候选 event_id。

    Args:
        entity_names: 查询里抽到的实体名。
        namespace: 记忆域（Cypher 里强制参与匹配）。
        settings: Neo4j 连接配置。
        limit: 最多返回条数。
        timeout_s: 超时秒数（ADR 200ms）。
        driver: 可注入的 driver（测试用）。

    Returns:
        GraphHits；**不抛异常**，失败返回 ``ok=False``。
    """
    names = _clean_names(entity_names)
    if not names:
        return GraphHits(event_ids=[], ok=True)

    owns_driver = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
                connection_timeout=timeout_s,
            )

        with drv.session() as session:
            result = session.run(
                _SEARCH_CYPHER,
                namespace=namespace,
                names=names,
                limit=int(limit),
                timeout=timeout_s,
            )
            event_ids = [row["event_id"] for row in result if row.get("event_id")]
        return GraphHits(event_ids=event_ids, ok=True)
    except Exception as exc:  # noqa: BLE001 - 图失败必须降级，不能抛给上层
        message = f"图扩展失败（已降级）: {exc}"
        logger.warning("%s namespace=%s", message, namespace)
        return GraphHits(event_ids=[], ok=False, error=str(exc))
    finally:
        if owns_driver and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 Neo4j driver 失败", exc_info=True)
