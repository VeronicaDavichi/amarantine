"""启动补跑 + 定期维护。

## 为什么这里可以用异步任务，而 MCP 进程里不行

判断标准不是「哪个更时髦」，是**「丢了会不会留下不可恢复的错误」**：

- **admin_http 里的维护任务**：状态全在 `pending_task` 表里，任务丢了
  下次 tick 接着做 —— **异步任务只是触发器，不是状态持有者**。
  进程被 SIGKILL 也不会丢任何东西，因为丢的只是一个 tick。
- **把「图写入」本身丢给 MCP 进程的后台线程**：那条事件的关系就永久
  缺失，因为 MCP 进程由 host 控制，随时可能被 SIGKILL，而我们抓不到。

一句话：**触发器可以丢，状态不能只存在于内存里。**

## 什么时候用这一层

| 场景 | 谁来做 |
|---|---|
| MCP 进程刚起来 | `startup_recover()`，同步扫一遍表 |
| admin_http 常驻 | `MaintenanceWorker.run_forever()`，30s 一次 |
| 跨库毫秒窗口 | `tick()` 里每天一次对账 |

两个入口都做启动补跑：MCP 可能根本没被拉起（用户没在用），
admin_http 可能没开（只是挂着记忆库）。谁先起来谁补。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from memory.config import Settings
from memory.services.pending_task import (
    PendingTaskStore,
    ReconcileReport,
    RetryReport,
    db_path_from_settings,
    reconcile_graph,
    retry_pending,
)

logger = logging.getLogger("memory.maintenance")

#: 补跑间隔（秒）
TICK_S = 30.0
#: 对账间隔（秒）—— O(n)，所以低频
RECONCILE_EVERY_S = 86400.0
#: 单次 tick 最多补几条（防止一条堆积把一次 tick 拖死）
RETRY_BATCH = 50


@dataclass
class TickReport:
    """一次 tick 的产出。"""

    retry: RetryReport = field(default_factory=RetryReport)
    reconcile: ReconcileReport | None = None

    @property
    def did_anything(self) -> bool:
        return self.retry.attempted > 0 or (
            self.reconcile is not None
            and (self.reconcile.checked > 0 or self.reconcile.queued > 0)
        )


def startup_recover(
    namespace: str,
    *,
    settings: Settings,
    store: PendingTaskStore | None = None,
    limit: int = RETRY_BATCH,
) -> RetryReport:
    """启动时扫一遍意图日志，把欠的图补上。

    **这是 SIGKILL 之后唯一的补课机会** —— 关闭时的钩子根本不会执行。
    同步调用，失败只告警（服务该起还是要起）。
    """
    try:
        store = store or PendingTaskStore(db_path_from_settings(settings))
        report = retry_pending(
            namespace, settings=settings, store=store, limit=limit
        )
        if report.attempted:
            logger.info(
                "启动补跑：尝试 %d，补上 %d，待重试 %d，放弃 %d",
                report.attempted,
                report.recovered,
                report.still_pending,
                report.dead,
            )
        return report
    except Exception as exc:  # noqa: BLE001 - 补课失败不该拦住启动
        logger.warning("启动补跑失败（已忽略）: %s", exc)
        return RetryReport()


class MaintenanceWorker:
    """定期补跑 + 每日对账。

    单线程同步 `tick()`，外面用 `run_forever()` 包成异步循环，
    或由调用方自己按需 tick（测试就这么用）。
    """

    def __init__(
        self,
        namespace: str,
        settings: Settings,
        *,
        store: PendingTaskStore | None = None,
        tick_s: float = TICK_S,
        reconcile_every_s: float = RECONCILE_EVERY_S,
        retry_batch: int = RETRY_BATCH,
        reconcile_on_start: bool = True,
    ) -> None:
        self._namespace = namespace
        self._settings = settings
        self._store = store or PendingTaskStore(db_path_from_settings(settings))
        self._tick_s = max(1.0, tick_s)
        self._reconcile_every_s = max(0.0, reconcile_every_s)
        self._retry_batch = max(1, retry_batch)
        # 启动后第一次 tick 就对账 —— 那是补跨库窗口最好的时机。
        # 测试里关掉：对账要连真 Weaviate / Neo4j，单测不该联网。
        self._next_reconcile_at: datetime | None = (
            None if reconcile_on_start else datetime.now(timezone.utc)
        )
        self._last_reconcile_at: datetime | None = None

    @property
    def store(self) -> PendingTaskStore:
        return self._store

    def _due_for_reconcile(self, moment: datetime) -> bool:
        if self._next_reconcile_at is None:
            return True
        return moment >= self._next_reconcile_at

    def tick(self, *, now: datetime | None = None) -> TickReport:
        """跑一次：补跑到期任务；到了对账时间就顺便对一次账。"""
        moment = now or datetime.now(timezone.utc)
        report = TickReport()

        try:
            report.retry = retry_pending(
                self._namespace,
                settings=self._settings,
                store=self._store,
                limit=self._retry_batch,
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 - 一个 tick 失败不能拖死循环
            logger.warning("补跑 tick 失败（已忽略）: %s", exc)

        if self._due_for_reconcile(moment):
            try:
                report.reconcile = reconcile_graph(
                    self._namespace,
                    settings=self._settings,
                    store=self._store,
                    now=moment,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("对账 tick 失败（已忽略）: %s", exc)
            self._last_reconcile_at = moment
            self._next_reconcile_at = moment + timedelta(
                seconds=self._reconcile_every_s
            )

        return report

    async def run_forever(self) -> None:
        """无限循环。任何异常都吞掉重来 —— 这个循环死了就没人补课了。"""
        logger.info(
            "维护循环启动 namespace=%s tick=%ss 对账=%ss",
            self._namespace,
            self._tick_s,
            self._reconcile_every_s,
        )
        while True:
            try:
                report = await asyncio.to_thread(self.tick)
                if report.did_anything:
                    logger.info(
                        "维护 tick：补跑 尝试%d/补上%d 对账 检查%d/入队%d",
                        report.retry.attempted,
                        report.retry.recovered,
                        report.reconcile.checked if report.reconcile else 0,
                        report.reconcile.queued if report.reconcile else 0,
                    )
            except asyncio.CancelledError:
                logger.info("维护循环收到取消，退出")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("维护循环异常（继续）: %s", exc)
            await asyncio.sleep(self._tick_s)
