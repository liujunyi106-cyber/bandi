# 功能：定时任务投递消费者——把投递箱里的到期任务交给会话主链路执行
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bandi.scheduler.store import DeliveryStore, TaskDelivery

logger = logging.getLogger(__name__)

# 功能：两次取投递的间隔——投递通常是低频事件，短轮询足够且开销可忽略
DEFAULT_POLL_INTERVAL_SEC = 15.0
# 功能：单批取几条——一条投递就是一轮完整 Agent 回合，串行执行，批量大会饿死主链
DEFAULT_BATCH_SIZE = 1


class DeliveryConsumer:
    """功能：消费定时任务投递，交给执行回调（与用户发消息同一条链路）。

    设计要点：
    - **不自动重试**：投递失败落 failed + 原因。投递失败通常是库不可用，
      重试只会放大故障（DSH 同口径：refused admission does not retry）。
    - **at-least-once**：执行完才标 consumed；未标记的保持 pending，
      重启后会被重新取到。
    - 单批串行：一条投递 = 一轮完整回合，并发跑多个回合会争抢同一会话槽。
    """

    def __init__(
        self,
        deliveries: DeliveryStore,
        execute: Callable[[TaskDelivery], Awaitable[str]],
        *,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._deliveries = deliveries
        self._execute = execute
        self._poll_interval = max(1.0, float(poll_interval_sec))
        self._batch_size = max(1, int(batch_size))
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("DeliveryConsumer started (interval=%ss)", self._poll_interval)

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wakeup(self) -> None:
        """功能：立即催一次消费（调度器刚入箱时调用，省一次轮询延迟）。"""
        self._wakeup.set()

    @property
    def running(self) -> bool:
        return self._running

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
            self._wakeup.clear()
            await self.drain()

    async def drain(self) -> int:
        """功能：把当前所有 pending 投递执行一遍，返回本轮处理条数。

        单条失败只落 failed，不中断其余投递（一个坏任务不能饿死队列）。
        """
        handled = 0
        while True:
            pending = self._deliveries.claim_pending(limit=self._batch_size)
            if not pending:
                break
            for delivery in pending:
                await self._run_one(delivery)
                handled += 1
            if not self._running:
                break
        return handled

    async def _run_one(self, delivery: TaskDelivery) -> None:
        try:
            output = await self._execute(delivery)
            self._deliveries.mark_consumed(delivery.delivery_id)
            logger.info(
                "delivery %s consumed (task=%s output=%d chars)",
                delivery.delivery_id,
                delivery.task_id,
                len(str(output or "")),
            )
        except asyncio.CancelledError:
            # 未标终态：保持 pending，重启后 at-least-once 重投
            raise
        except Exception as e:  # noqa: BLE001 — 单条投递失败不得中断队列
            logger.warning("delivery %s failed: %s", delivery.delivery_id, e)
            self._deliveries.mark_failed(delivery.delivery_id, str(e))
