"""遗忘扫描：拉卡片 → 查图 → 分类。一处实现，两个调用方共用。

## 为什么单独抽出来

原来 `/admin/review` 和 `/admin/sweep` 各有一份「拉卡片 + 查图」的代码，
而 review 那份**漏了查图** —— 于是 `GRAPH_HUB_DEGREE` 的枢纽保护在那个
端点从来没生效过，两个端点对同一批数据给出不同的分桶结果（spec 022 修的）。

只要还有两份实现，这类不一致就会再出现。所以合并到这里：
`MaintenanceWorker`（定时扫描）和 admin 的两个端点都用同一个 `scan()`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from memory.config import Settings
from memory.core.forget_policy import ForgetCandidate, SweepReport, sweep
from memory.services.graph_search import event_degrees, event_neighborhood
from memory.services.recall_count import RecallCounter
from memory.services.pending_task import db_path_from_settings
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger(__name__)


class IndexUnavailable(RuntimeError):
    """索引层（Weaviate）不可用。调用方决定映射成 HTTP 503 还是日志。"""


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_candidates(settings: Settings, namespace: str) -> list[ForgetCandidate]:
    """从 Weaviate 拉未删除的卡，补上召回统计，转成分类输入。

    `last_recalled_at` 不在 Weaviate schema 里（召回计数是派生数据，
    特意放独立 SQLite 表避免写放大），所以从 `RecallCounter` 补。
    """
    store = WeaviateStore(settings)
    try:
        rows = store.list_all(namespace)
    except WeaviateStoreError as exc:
        raise IndexUnavailable(f"索引层不可用: {exc}") from exc
    finally:
        store.close()

    try:
        counter = RecallCounter(db_path_from_settings(settings))
    except Exception:  # noqa: BLE001 - 计数表不可用不该让整次扫描失败
        logger.warning("召回计数表不可用，last_recalled_at 将缺失", exc_info=True)
        counter = None

    out: list[ForgetCandidate] = []
    for r in rows:
        event_id = r["event_id"]
        count = int(r.get("recall_count") or 0)
        last_at = None
        if counter is not None:
            try:
                count, raw = counter.stats(namespace, event_id)
                last_at = _parse_date(raw)
            except Exception:  # noqa: BLE001
                pass
        out.append(
            ForgetCandidate(
                event_id=event_id,
                importance=float(r.get("importance") or 0.0),
                summary=r.get("summary"),
                recall_count=count,
                created_at=_parse_date(r.get("date_start")),
                last_recalled_at=last_at,
                retention=str(r.get("retention") or "normal"),
                pinned=bool(r.get("pinned")),
                unresolved=list(r.get("unresolved") or []),
                confirmed_by_use=int(r.get("confirmed_by_use") or 0),
                review_verdict=r.get("review_verdict"),
                # graph_degree / neighbor_events 不在这里填 —— 它们要查 Neo4j，
                # 属于 IO，由 scan() 批量查。留空 = 图不可用时的默认值。
            )
        )
    return out


@dataclass
class ScanResult:
    """一次扫描的完整产出。"""

    report: SweepReport
    #: 本次参与分类的卡片数
    total: int = 0
    #: namespace 里的 Graph Event 节点总数（孤岛判定的数据量门槛用）
    namespace_events: int = 0
    #: 图不可用时为 True —— 枢纽保护和孤岛判定都没生效
    graph_degraded: bool = False
    #: 分类用的候选（调用方需要 summary 之类的原始字段时用）
    candidates: list[ForgetCandidate] = field(default_factory=list)

    @property
    def buckets(self) -> dict[str, int]:
        return self.report.buckets


def scan(
    settings: Settings,
    namespace: str,
    *,
    rows: list[ForgetCandidate] | None = None,
    today: date | None = None,
    limit: int | None = None,
    degrees: dict[str, int] | None | object = ...,
    neighborhood: Any = ...,
) -> ScanResult:
    """拉卡片 → 查图（度数 + 邻域）→ 分类。**只出报告，不删任何东西**。

    `sweep()` 内部强制 `dry_run=True`（A12 闸门未过前不允许自动删除），
    这里不绕过它。

    Args:
        settings: 配置。
        namespace: 记忆域。
        rows: 已加载的候选。为 None 时自己调 `load_candidates()`。
             admin 端点传已加载的（它们有自己的 `_load_all`，测试会 monkeypatch），
             `MaintenanceWorker` 不传（自己去拉）。
        today: 注入的「今天」（测试用）。
        limit: 只处理前 N 张卡（admin 端点用）。
        degrees / neighborhood: 测试注入用。传 `...` 表示自己去查图。

    Raises:
        IndexUnavailable: Weaviate 不可用。
    """
    if rows is None:
        rows = load_candidates(settings, namespace)
    if limit is not None:
        rows = rows[:limit]

    ids = [c.event_id for c in rows]

    # 第 4 类硬信号：图度数。图不可用时为 None —— 一律按 0 继续，
    # 但标 graph_degraded，不能让「少了一类保护」静默通过。
    if degrees is ...:
        deg_map = event_degrees(ids, namespace, settings=settings)
    else:
        deg_map = degrees
    if deg_map is None:
        graph_degraded = True
    else:
        graph_degraded = False
        rows = [
            replace(c, graph_degree=deg_map.get(c.event_id, 0)) for c in rows
        ]

    # 孤岛判定（spec 022）：事件级邻域 + namespace 事件总数。
    if neighborhood is ...:
        neigh = event_neighborhood(ids, namespace, settings=settings)
    else:
        neigh = neighborhood
    namespace_events = 0
    if neigh is None:
        graph_degraded = True
    else:
        namespace_events = neigh.total_events
        rows = [
            replace(c, neighbor_events=neigh.neighbors.get(c.event_id, 0))
            for c in rows
        ]

    report = sweep(
        rows,
        today=today or date.today(),
        idle_days=settings.FORGET_IDLE_DAYS,
        min_recalls=settings.FORGET_MIN_RECALLS,
        grace_days=settings.FORGET_GRACE_DAYS,
        protect_importance=settings.PROTECT_IMPORTANCE_REVIEW,
        graph_hub_degree=settings.GRAPH_HUB_DEGREE,
        confirmed_min=settings.CONFIRMED_MIN,
        island_max_neighbors=settings.ISLAND_MAX_NEIGHBORS,
        island_min_events=settings.ISLAND_MIN_EVENTS,
        namespace_events=namespace_events,
    )

    return ScanResult(
        report=report,
        total=len(rows),
        namespace_events=namespace_events,
        graph_degraded=graph_degraded,
        candidates=rows,
    )
