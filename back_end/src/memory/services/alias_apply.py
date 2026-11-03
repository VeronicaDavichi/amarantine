"""别名的推断编排：读卡 → 找存疑 claim → LLM 解析 → 写回卡和图。

`alias_resolver` 只管「一条 claim 能不能解析出来」，
这里管「从哪读、写到哪、失败了怎么办」。

## 为什么是手动触发而不是写入时自动跑

`remember` 是热路径，一次 LLM 调用要 1-2 秒。同步塞进去会让
「记一句话」变成「等两秒」。

而且别名推断**不是写入的一部分**：事件卡已经完整了，别名只是派生关系。
推断失败、延迟、重跑都不该影响记忆本身。

所以做成独立动作，由 `POST /admin/aliases/resolve` 触发。
后面要改成后台任务也只需要换个调度器，这块逻辑不用动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory.config import Settings
from memory.models.event_card import AliasClaim
from memory.services.alias_reject import record_safe
from memory.services.alias_resolver import resolve_claims
from memory.services.graph_outbox import append_alias_edges, list_known_names
from memory.services.weaviate_store import WeaviateStore

logger = logging.getLogger(__name__)


@dataclass
class ResolveReport:
    """一次解析任务的结果，给调用方看。"""

    event_id: str
    scanned: int = 0
    resolved: list[AliasClaim] = field(default_factory=list)
    written_to_card: int = 0
    written_to_graph: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "scanned": self.scanned,
            "resolved": [
                {
                    "alias": c.alias,
                    "canonical": c.canonical,
                    "label": c.label,
                    "source": c.source,
                }
                for c in self.resolved
            ],
            "written_to_card": self.written_to_card,
            "written_to_graph": self.written_to_graph,
            "error": self.error,
        }


def _load_claims(card: dict[str, Any]) -> list[AliasClaim]:
    """从卡里读出 claim 列表。坏数据跳过而不是炸掉。"""
    import json

    raw = card.get("alias_claims")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("alias_claims 不是合法 JSON，跳过 | %s", card.get("event_id"))
        return []
    if not isinstance(parsed, list):
        return []

    claims: list[AliasClaim] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            claims.append(
                AliasClaim(
                    alias=str(item["alias"]),
                    canonical=str(item["canonical"]),
                    label=item.get("label", "uncertain"),
                    source=item.get("source", "user_declared"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return claims


def resolve_event_aliases(
    namespace: str,
    event_id: str,
    *,
    settings: Settings,
    store: WeaviateStore | None = None,
    driver: object | None = None,
    llm_client: Any | None = None,
    timeout_s: float | None = None,
    known_names: list[str] | None = None,
) -> ResolveReport:
    """对一张卡跑别名推断。

    `known_names` 可以外部传入（省一次图查询）；不传就从图上现取。
    """
    report = ResolveReport(event_id=event_id)
    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    try:
        if owns_store:
            active_store.ensure_collection()

        card = active_store.get_by_event_id(namespace, event_id)
        if card is None:
            report.error = "事件卡不存在"
            return report

        claims = _load_claims(card)
        report.scanned = len(claims)
        if not claims:
            return report

        # 规则层的原 claim 永远停在 `uncertain` —— 它不会被推断结果改写。
        # 所以每跑一次就会重新推断一次，第二次推出来的置信度可能跟第一次
        # 不一样（LLM 不是确定性的），于是卡上长出
        # `糖包 -> 豆包 uncertain` + `糖包 -> 豆包 probable` 两条。
        #
        # 判据是「这个 alias 已经推断过了」而不是「这个 canonical 推过了」：
        # 推断前根本不知道会落到哪个 canonical 上。
        already = {c.alias for c in claims if c.source == "llm_inferred"}
        todo = [
            c for c in claims if c.label == "uncertain" and c.alias not in already
        ]
        if not todo:
            if already:
                logger.info(
                    "别名已推断过，跳过 | event_id=%s | %s", event_id, sorted(already)
                )
            return report

        candidates = (
            known_names
            if known_names is not None
            else list_known_names(namespace, settings=settings, driver=driver)
        )
        if not candidates:
            logger.info("图上没有候选对象，跳过别名推断 | %s", event_id)
            return report

        resolved = resolve_claims(
            todo,
            candidates=candidates,
            settings=settings,
            client=llm_client,
            timeout_s=timeout_s,
        )
        report.resolved = resolved
        if not resolved:
            return report

        # 先写图再写卡：图失败只告警，卡上的留痕还要继续
        now = datetime.now(timezone.utc).isoformat()
        write = append_alias_edges(
            event_id,
            namespace,
            resolved,
            settings=settings,
            created_at=now,
            driver=driver,
        )
        report.written_to_graph = write.written
        # **LLM 决议过、但判据仍然不认的，也要留痕。**
        #
        # 这是原来说不通的一环：LLM 只处理 `uncertain` claim（所以它能看见
        # 右值），可它决议完之后，写图的 `_partition_alias_claims` 会**再**
        # 用同一套判据拒一次 —— 于是「LLM 说该建，代码说不建」这件事
        # 完全不留痕迹。现在它进 `alias_reject`，带 reason。
        if write.skipped:
            record_safe(
                settings,
                namespace=namespace,
                event_id=event_id,
                rejects=[s.to_dict() for s in write.skipped],
                source_text=f"llm_inferred:{event_id}",
            )

        report.written_to_card = active_store.append_alias_claims(
            namespace,
            event_id,
            [
                {
                    "alias": c.alias,
                    "canonical": c.canonical,
                    "label": c.label,
                    "source": c.source,
                }
                for c in resolved
            ],
        )
        return report
    except Exception as exc:  # noqa: BLE001 - 推断失败不能拖垮调用方
        logger.warning("别名推断失败 | event_id=%s | %s", event_id, exc)
        report.error = str(exc)
        return report
    finally:
        if owns_store:
            try:
                active_store.close()
            except Exception:  # noqa: BLE001
                pass


def resolve_namespace_aliases(
    namespace: str,
    *,
    settings: Settings,
    store: WeaviateStore | None = None,
    driver: object | None = None,
    llm_client: Any | None = None,
    timeout_s: float | None = None,
    limit: int = 20,
) -> list[ResolveReport]:
    """扫一个命名空间，对**含存疑 claim** 的卡逐张推断。

    `limit` 是卡数上限 —— 一次 LLM 调用 1-2 秒，几十张就是分钟级，
    接口不能无限等。超出的下次再扫。
    """
    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    try:
        if owns_store:
            active_store.ensure_collection()
        rows = active_store.list_all(namespace, limit=limit)
        report_list: list[ResolveReport] = []
        # 候选名单只取一次，省掉每张卡一次图查询
        candidates = list_known_names(namespace, settings=settings, driver=driver)
        for row in rows:
            claims = _load_claims(row)
            if not any(c.label == "uncertain" for c in claims):
                continue
            report_list.append(
                resolve_event_aliases(
                    namespace,
                    row["event_id"],
                    settings=settings,
                    store=active_store,
                    driver=driver,
                    llm_client=llm_client,
                    timeout_s=timeout_s,
                    known_names=candidates,
                )
            )
        return report_list
    finally:
        if owns_store:
            try:
                active_store.close()
            except Exception:  # noqa: BLE001
                pass
