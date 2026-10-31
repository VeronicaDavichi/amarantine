"""E2 `memory_feedback`：引用回路。

## 这个工具回答什么

召回给了 10 条材料，LLM 到底用了几条？只有调用方知道。所以
`confirmed_by_use` **只能**由这个工具推进（C.B6 禁止自动判定）。

这个数字是 D7 的第五个硬保护信号：被真正引用过的卡不该被遗忘。

## 三条硬约束（照 ADR 抄下来，别改）

1. **幂等键 `(trace_id, event_id)`** —— 同一次召回里同一条材料报两次，
   第二次计入 `deduped`，`confirmed_by_use` **不累加**
2. **只接受该 trace 实际返回过的 id** —— 否则整个请求 `isError`
3. **整单校验** —— 有一条非法就全拒，不做部分成功

第 3 条为什么不做成部分成功：部分成功之后 LLM 不知道哪几条生效了，
重试时会把已经成功的又报一遍（虽然幂等能兜住，但 `deduped` 会虚高），
而且「成功了一半」这种返回值没法让调用方判断该不该重试。

## 为什么 `confirmed_by_use` 写绝对值而不是 +1

真值在 SQLite（幂等键保证不重复），Weaviate 只是镜像。写绝对值能让
并发下丢失的更新在下次自动修正；写 `+1` 丢了就永远丢了。
"""

from __future__ import annotations

import logging
from typing import Any

from memory.config import Settings
from memory.services.recall_trace import (
    FeedbackOutcome,
    RecallTraceStore,
    trace_store_from_settings,
)
from memory.services.weaviate_store import WeaviateStore

logger = logging.getLogger("memory.feedback")


class FeedbackError(RuntimeError):
    """feedback 请求不合法（不是系统故障，调用方应该修正参数重试）。"""


def submit_feedback(
    *,
    namespace: str,
    trace_id: str,
    used_material_ids: list[str],
    settings: Settings,
    trace_store: RecallTraceStore | None = None,
    store: WeaviateStore | None = None,
) -> FeedbackOutcome:
    """处理一次引用上报。

    Args:
        namespace: 记忆域，必须是 trace 原本所属的那个。
        trace_id: `memory_recall` 返回的 trace_id。
        used_material_ids: 本次真正引用了的材料 event_id 列表。
        trace_store: 可注入的 trace 存储（测试用）。
        store: 可注入的 Weaviate store（测试用）。

    Returns:
        FeedbackOutcome，`to_tool_result()` 给出工具返回体。

    Raises:
        FeedbackError: trace 不存在 / namespace 不匹配 / 出现不属于该 trace 的 id。
    """
    if not trace_id or not trace_id.strip():
        raise FeedbackError("trace_id 不能为空")
    if not isinstance(used_material_ids, list) or not used_material_ids:
        raise FeedbackError("used_material_ids 必须是非空数组")

    # 去重保序：同一批里重复报同一条只处理一次
    ids: list[str] = []
    for raw in used_material_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise FeedbackError(f"used_material_ids 含非法项: {raw!r}")
        eid = raw.strip()
        if eid not in ids:
            ids.append(eid)

    owns_trace = trace_store is None
    active_trace = trace_store or trace_store_from_settings(settings)
    del owns_trace  # 存储是轻量连接，不需要显式关闭

    record = active_trace.get_trace(trace_id)
    if record is None:
        raise FeedbackError(f"trace_id 不存在或已过期: {trace_id}")
    if record.namespace != namespace:
        # 跨域引用一律拒绝：不泄露别的 namespace 有没有这张卡
        raise FeedbackError(f"trace_id 不属于当前记忆域: {trace_id}")

    # 整单校验：有一条不在返回集里就全拒
    allowed = set(record.material_ids)
    rejected = [eid for eid in ids if eid not in allowed]
    if rejected:
        raise FeedbackError(
            "以下 event_id 不在该 trace 的返回集里: " + ", ".join(rejected[:5])
        )

    outcome = FeedbackOutcome()
    for eid in ids:
        inserted = active_trace.add_feedback(
            trace_id=trace_id, namespace=namespace, event_id=eid
        )
        if not inserted:
            outcome.deduped += 1
            continue
        outcome.accepted += 1
        outcome.incremented.append(eid)

    if not outcome.incremented:
        return outcome

    # 同步镜像到 Weaviate（写绝对值）。失败只 warn：真值已经在 SQLite 里，
    # 丢一次镜像不影响正确性，下次 feedback 会覆盖上来。
    owns_store = store is None
    active_store = store or WeaviateStore(settings)
    try:
        for eid in outcome.incremented:
            count = active_trace.count_feedback(namespace, eid)
            try:
                active_store.set_confirmed_by_use(eid, namespace, count=count)
            except Exception as exc:  # noqa: BLE001 - 镜像失败不影响已记账的结果
                logger.warning(
                    "confirmed_by_use 镜像失败（真值已在 SQLite）: %s %s", eid, exc
                )
    finally:
        if owns_store:
            active_store.close()

    return outcome
