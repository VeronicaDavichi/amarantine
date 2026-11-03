"""D7 软删（A32）。

ADR 的 N4 很明确：**事件卡与原文永不物理删除**。
所以 `memory_forget` 做的不是 DELETE，而是：

1. 在 Weaviate 对象上写 `deleted_at`（移出可检索集合）
2. 图节点标记 `retention = "forgotten"`
3. 事实层（原文 / raw_quote）**一动不动**

`memory_recall` 的 where 里恒带 `deleted_at IsNull`，
所以忘掉之后检索不到，但审计通道仍能读到 —— 这就是 A32 要的对称性。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory.config import Settings
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger("memory.forget")

#: reason 全集。
#:
#: MCP `memory_forget` 只暴露前两个（ADR 4.9）；
#: `retention_expired` 是管理侧 `DELETE /admin/event/{event_id}` 专用（ADR 4.7），
#: 表示「不是因为用户开口，而是保留期到了」—— 审计上必须和 `user_forget`
#: 分得开，所以不能把管理侧的原因硬映射成 `outdated`。
ALLOWED_REASONS = ("user_forget", "outdated", "retention_expired")

#: MCP 工具允许的 reason 子集（入口自己收窄，不靠 service 拦）。
MCP_REASONS = ("user_forget", "outdated")

#: 管理侧 `DELETE /admin/event/{event_id}` 允许的 reason（ADR 4.7）。
#:
#: **不含 `outdated`**：那是 MCP 的语义（内容过时了），
#: 管理面删卡只有「用户开口」和「保留期到了」两种。
ADMIN_REASONS = ("user_forget", "retention_expired")


class ForgetError(RuntimeError):
    """遗忘失败（对象不存在、Weaviate 不可用等）。"""


@dataclass(frozen=True)
class ForgetResult:
    event_id: str
    forgotten: bool
    reason: str
    graph_ok: bool

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "forgotten": self.forgotten,
            "reason": self.reason,
        }


def forget_event(
    *,
    namespace: str,
    event_id: str,
    reason: str,
    settings: Settings,
    store: WeaviateStore | None = None,
    graph_writer: Any | None = None,
    now: datetime | None = None,
) -> ForgetResult:
    """软删一条记忆。

    Args:
        namespace: 记忆域（必须与写入时一致，否则查不到）。
        event_id: 事件 id。
        reason: `user_forget` 或 `outdated`。
        settings: 配置。
        store: 可注入的 Weaviate 封装。
        graph_writer: 可注入的图写入器（`(namespace, event_id, retention)`）。
        now: 时刻，缺省 UTC。

    Raises:
        ValueError: reason 不在枚举内 / event_id 为空。
        ForgetError: 目标不存在或索引层不可用。
    """
    if reason not in ALLOWED_REASONS:
        raise ValueError(f"reason 必须是 {ALLOWED_REASONS} 之一，收到 {reason!r}")
    if not event_id or not event_id.strip():
        raise ValueError("event_id 不能为空")

    moment = now or datetime.now(timezone.utc)
    owns_store = store is None
    active = store or WeaviateStore(settings)

    try:
        try:
            hit = active.get_by_event_id(namespace, event_id)
        except WeaviateStoreError as exc:
            raise ForgetError(f"索引层不可用: {exc}") from exc
        if hit is None:
            raise ForgetError(f"找不到 event_id={event_id}（namespace={namespace}）")

        if hit.get("is_deleted"):
            # 已经忘过了，幂等返回成功
            return ForgetResult(event_id=event_id, forgotten=True, reason=reason, graph_ok=True)

        try:
            active.mark_forgotten(event_id, namespace, deleted_at=moment.isoformat())
        except WeaviateStoreError as exc:
            raise ForgetError(f"标记软删失败: {exc}") from exc
    finally:
        if owns_store:
            active.close()

    # 图节点失败只告警（D2：图可丢弃重建，不能阻塞主流程）
    graph_ok = True
    if graph_writer is not None:
        try:
            graph_writer(namespace, event_id, reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning("图节点标记遗忘失败（已忽略）: %s", exc)
            graph_ok = False

    return ForgetResult(
        event_id=event_id, forgotten=True, reason=reason, graph_ok=graph_ok
    )
