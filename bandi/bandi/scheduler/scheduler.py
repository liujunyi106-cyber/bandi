# 功能：定时任务调度循环（复用 run_tick_loop 骨架 + interval 钳制 + 幂等启停）
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bandi.company.heartbeat import run_tick_loop
from bandi.scheduler.cron import CronExpr
from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore

logger = logging.getLogger(__name__)

MIN_TICK_INTERVAL_SEC = 1.0


@dataclass
class SchedulerConfig:
    """"""

    tick_interval_sec: float = 30.0
    now_fn: Callable[[], datetime] = datetime.now
    # 单任务执行上限：挂死任务超时判 FAILED，不阻塞 tick 循环其余任务
    task_timeout_sec: float = 300.0
    # 10.3 catch-up：失败窗口有界重试次数——失败不立即推进 next_run_at
    # 下个 tick 重试同一窗口；超限后推进（不丢窗口、不重试风暴）
    max_window_retries: int = 2
    # （测试/未装配 → None 即零行为）；维护失败不得中断 tick 循环
    maintenance_enabled: bool = True
    maintenance_interval_sec: float = 86400.0
    maintenance_callable: Callable[[], Any] | None = None


def is_silent_completion(result: Any) -> bool:
    """功能（10.3 静默完成协议）：空输出 / HEARTBEAT_OK 标记视为「无事可报」
    不写入用户可见产出（openclaw HEARTBEAT_OK 静默剥离语义）。"""
    if result is None:
        return True
    text: Any = result
    if isinstance(result, dict):
        for key in ("output", "content", "message", "summary"):
            if key in result:
                text = result[key]
                break
    normalized = str(text or "").strip()
    if not normalized:
        return True
    return normalized.upper() in ("HEARTBEAT_OK", "[HEARTBEAT_OK]")


class TaskScheduler:
    """进程内 asyncio 后台任务：按 cron 到期触发 runner（headless agent turn）。

    runner 签名：Callable[[ScheduledTask], Awaitable[Any]]。触发失败有界重试
    同一窗口（max_window_retries）后推进 next_run_at（catch-up 不丢窗口
    防重试风暴）；runner 产出经静默协议落 last_output（空/HEARTBEAT_OK 不可见）。
    启停幂等。
    """

    def __init__(
        self,
        store: TaskStore,
        runner: Callable[[ScheduledTask], Awaitable[None]],
        config: SchedulerConfig | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self._config = config or SchedulerConfig()
        self._config.tick_interval_sec = float(
            max(MIN_TICK_INTERVAL_SEC, self._config.tick_interval_sec)
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._wakeup_event = asyncio.Event()
        self._window_retries: dict[str, int] = {}
        self._last_maintenance_at: datetime | None = None
        self._maintenance_retry_at: datetime | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("TaskScheduler started (interval=%ss)", self._config.tick_interval_sec)

    async def stop(self) -> None:
        self._running = False
        self._wakeup_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def running(self) -> bool:
        """后台 tick 循环是否在跑（API 状态面消费）。"""
        return self._running

    # -- loop ---------------------------------------------------------------

    async def _tick_loop(self) -> None:
        await run_tick_loop(
            running=lambda: self._running,
            tick=self._tick,
            wait=self._wait_next_tick,
            loop_name="TaskScheduler",
            loop_logger=logger,
        )

    async def _wait_next_tick(self) -> None:
        try:
            await asyncio.wait_for(
                self._wakeup_event.wait(), timeout=self._config.tick_interval_sec
            )
        except TimeoutError:
            pass
        self._wakeup_event.clear()

    def wakeup(self) -> None:
        """立即触发一次 tick（不等待正常周期）。需在事件循环上下文内调用。"""
        self._wakeup_event.set()

    # -- tick ---------------------------------------------------------------

    async def _tick(self) -> None:
        """到期任务触发一次并推进 next_run_at；单个失败不吞 tick。"""
        now = self._config.now_fn()
        for task in self.store.list():
            if not task.enabled:
                continue
            if task.next_run_at is None:
                continue
            if task.next_run_at > now:
                continue
            await self._fire(task, now)
        await self._maybe_run_maintenance(now)

    async def _maybe_run_maintenance(self, now: datetime) -> None:
        """功能：维护钩子——按间隔（默认每日）在 executor 执行注入的 callable。

        成功才记正常间隔；失败只告警不中断 tick，并按
        max(60, interval/10) 秒安排短周期重试（不风暴）；未配置/显式关闭 → 零行为。
        """
        callable_ = self._config.maintenance_callable
        if not self._config.maintenance_enabled or callable_ is None:
            return
        if self._maintenance_retry_at is not None:
            if now < self._maintenance_retry_at:
                return
        elif self._last_maintenance_at is not None:
            elapsed = (now - self._last_maintenance_at).total_seconds()
            if elapsed < max(0.0, float(self._config.maintenance_interval_sec)):
                return
        try:
            # 同步维护（sqlite/文件 IO）不得阻塞事件循环
            await asyncio.to_thread(callable_)
        except Exception as e:  # noqa: BLE001 — 维护失败不得中断 tick 循环
            logger.warning("scheduler maintenance failed: %s", e)
            interval = max(0.0, float(self._config.maintenance_interval_sec))
            retry_delay = max(60.0, interval / 10.0)
            self._maintenance_retry_at = now + timedelta(seconds=retry_delay)
            return
        self._maintenance_retry_at = None
        self._last_maintenance_at = now

    async def _fire(self, task: ScheduledTask, now: datetime) -> None:
        if not self.store.try_begin_run(task.task_id):
            # next_run（下个 tick 到期重试），防同任务双跑
            return
        try:
            result = await asyncio.wait_for(
                self.runner(task), timeout=self._config.task_timeout_sec
            )
        except asyncio.CancelledError:
            # 否则 DB 永久 running（重启后 try_begin_run 恒拒绝，任务卡死）
            self.store.set_status(
                task.task_id, TaskStatus.IDLE, error="cancelled by scheduler stop"
            )
            raise
        except TimeoutError:
            error = f"timeout after {self._config.task_timeout_sec}s"
            self._handle_failure(task, error, now)
            # 功能：超时后状态必须离开 running，否则 try_begin_run 永久拒绝
            self.store.set_status(task.task_id, TaskStatus.FAILED, error=error)
            return
        except Exception as e:  # noqa: BLE001 — 单任务失败不得中断 tick
            self._handle_failure(task, str(e), now)
            self.store.set_status(task.task_id, TaskStatus.FAILED, error=str(e))
            return
        self.store.set_status(task.task_id, TaskStatus.IDLE)
        self.store.set_last_run(task.task_id, now)
        # 10.3：静默完成协议——空/HEARTBEAT_OK 产出落空串（用户面不可见）
        self.store.set_last_output(
            task.task_id, "" if is_silent_completion(result) else str(result)[:2000]
        )
        self._window_retries.pop(task.task_id, None)
        self._advance(task, now)

    def _handle_failure(self, task: ScheduledTask, error: str, now: datetime) -> None:
        logger.warning("scheduled task %s failed: %s", task.task_id, error)
        self.store.set_status(task.task_id, TaskStatus.FAILED, error=error)
        retries = self._window_retries.get(task.task_id, 0) + 1
        if retries <= self._config.max_window_retries and task.next_run_at is not None:
            # catch-up：窗口不推进，下个 tick 重试同一次到期（有界，防重试风暴）
            self._window_retries[task.task_id] = retries
            self.store.set_next_run(task.task_id, task.next_run_at)
            return
        self._window_retries.pop(task.task_id, None)
        self._advance(task, now)

    def _advance(self, task: ScheduledTask, now: datetime) -> None:
        # 无论成败推进窗口（失败已通过 _handle_failure 走完有界重试）
        kind = str(getattr(task, "schedule_kind", "") or "cron")
        if kind == "cron":
            nxt = CronExpr(task.cron).next_after(now)
        elif kind == "once":
            nxt = None
        else:
            from bandi.scheduler.rules import compute_next_run

            nxt = compute_next_run(
                kind=kind,
                spec=dict(getattr(task, "schedule_spec", None) or {}),
                base=now,
                effective_from=getattr(task, "effective_from", "") or "",
                effective_until=getattr(task, "effective_until", "") or "",
                anchor=(task.created_at or now).date(),
            )
        self.store.set_next_run(task.task_id, nxt)
        if nxt is None:
            self.store.set_enabled(task.task_id, False)
