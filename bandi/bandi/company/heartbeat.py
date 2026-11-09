# 功能：心跳调度——周期唤醒角色执行 + 停滞检测

"""Heartbeat scheduler for company-mode agent autonomy.

作为进程内 asyncio 后台任务运行，无需独立服务。
适配：角色启用方式用构造白名单（Bandi OrgRole 无 heartbeat_enabled 属性）；
执行路径复用 CompanyDispatcher.claim/run_work_item；peer wait 停滞仅检测+报告
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bandi.company.company_mode import CompanyDispatcher
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item_transition import detect_stagnant_work_items

logger = logging.getLogger(__name__)


async def run_tick_loop(
    running: Callable[[], bool],
    tick: Callable[[], Awaitable[None]],
    wait: Callable[[], Awaitable[None]],
    *,
    loop_name: str,
    loop_logger: logging.Logger,
) -> None:
    """周期调度循环共享骨架（DRY）：tick 容错 + 等待节流。

    供 HeartbeatScheduler / CommsReactivationSweeper 复用；wait 由调用方
    注入（Event.wait_for 或 asyncio.sleep）。
    """
    while running():
        try:
            await tick()
        except asyncio.CancelledError:
            break
        except Exception:
            loop_logger.exception(f"{loop_name} tick error")
        try:
            await wait()
        except asyncio.CancelledError:
            break
        except TimeoutError:
            pass


class HeartbeatScheduler:
    """In-process heartbeat loop that periodically activates company roles."""

    def __init__(
        self,
        store: WorkItemStore,
        org_engine: OrgEngine,
        dispatcher: CompanyDispatcher,
        *,
        heartbeat_role_ids: set[str] | None = None,
        interval_sec: int | float = 30,
        max_concurrent_runs: int = 1,
        stale_waits_minutes: int = 30,
        communication: Any | None = None,
    ) -> None:
        self.store = store
        self.org_engine = org_engine
        self.dispatcher = dispatcher
        self.heartbeat_role_ids = heartbeat_role_ids
        self.interval_sec = float(max(1.0, interval_sec))
        # `len(_active_runs) >= 0` 恒真 → break 永不调度（心跳静默失效）；
        # 语义与 interval_sec 钳制一致：至少允许 1 个并发 run
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        self.stale_waits_minutes = stale_waits_minutes
        self.communication = communication
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._active_runs: dict[str, asyncio.Task[None]] = {}
        self._wakeup_event = asyncio.Event()
        self._last_heartbeat: dict[str, datetime] = {}
        self._active_item_ids: set[str] = set()
        self.last_stale_report: list[dict[str, Any]] = []
        self.last_deadlock_report: list[tuple[str, str]] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("HeartbeatScheduler started (interval=%ss)", self.interval_sec)

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
        for task in list(self._active_runs.values()):
            task.cancel()
        for task in list(self._active_runs.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — 停止时吞掉任务异常
                pass
        self._active_runs.clear()
        self._active_item_ids.clear()
        logger.info("HeartbeatScheduler stopped")

    # -- on-demand wakeup --------------------------------------------------

    def _has_active_run(self, agent_id: str) -> bool:
        return agent_id in self._active_runs and not self._active_runs[agent_id].done()

    def wakeup(self, agent_id: str, reason: str = "on_demand") -> None:
        """立即唤醒单个角色，不等待正常周期。

        注意：必须在 asyncio 事件循环上下文内调用（内部使用
        ``asyncio.create_task``）；从非 loop 线程调用会抛 RuntimeError。
        """
        logger.info("Wakeup requested for agent=%s reason=%s", agent_id, reason)
        if self._has_active_run(agent_id):
            logger.debug("Agent %s already has an active run, skipping wakeup", agent_id)
            return
        # 全局并发上限（与 _tick 同口径）——防 wakeup 旁路无上限起后台 run
        if len(self._active_runs) >= self.max_concurrent_runs:
            logger.debug(
                "Global concurrency cap reached (%s), skipping wakeup for %s",
                self.max_concurrent_runs,
                agent_id,
            )
            return
        self._active_runs[agent_id] = asyncio.create_task(self._run_agent_heartbeat(agent_id))

    # -- main loop ---------------------------------------------------------

    async def _tick_loop(self) -> None:
        await run_tick_loop(
            running=lambda: self._running,
            tick=self._tick,
            wait=self._wait_next_tick,
            loop_name="HeartbeatScheduler",
            loop_logger=logger,
        )

    async def _wait_next_tick(self) -> None:
        await asyncio.wait_for(self._wakeup_event.wait(), timeout=self.interval_sec)
        self._wakeup_event.clear()

    async def _resolve_stale_waits(self) -> None:
        """

        有 communication 时：对 WAITING_FOR_PEER 任务调 resolve_task_peer_wait
        （回复到达即解除；超时按 on_timeout 三分支：manager/meeting/continue）
        解除后落盘；无 communication 时保持轻量报告（历史行为）。
        """
        stale: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        waiting = [item for item in self.store.list_all() if item.phase == Phase.WAITING_FOR_PEER]
        if self.communication is not None:
            for item in waiting:
                if item.metadata.get("dependency_settlement"):
                    continue
                if self.communication.resolve_task_peer_wait(item):
                    self.store.save(item)
            waiting = [item for item in waiting if item.phase == Phase.WAITING_FOR_PEER]
        for item in waiting:
            if item.metadata.get("dependency_settlement"):
                continue
            updated = item.updated_at
            # `now - naive` 抛 TypeError 吞掉整个 tick
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated is None:
                continue
            elapsed = (now - updated).total_seconds() / 60
            if elapsed >= self.stale_waits_minutes:
                stale.append(
                    {
                        "id": item.id,
                        "phase": item.phase.value,
                        "elapsed_minutes": round(elapsed, 1),
                    }
                )
        self.last_stale_report = stale
        # 相位交集组装（同一张卡相位互斥 → 恒空转，互等环永不报告）
        if self.communication is not None:
            waiting_for = self.communication.build_waiting_graph(waiting)
            cycles = self.communication.detect_deadlocks(waiting_for)
            if cycles:
                logger.warning("deadlock cycles detected: %s", cycles)
            self.last_deadlock_report = cycles

    async def _tick(self) -> None:
        self._cleanup_done_runs()
        await self._resolve_stale_waits()
        stagnant = detect_stagnant_work_items(self.store)
        if stagnant:
            logger.warning("stagnant work items: %s", [s["id"] for s in stagnant])

        if self.communication is not None:
            waiting_for_meeting = [
                item
                for item in self.store.list_all()
                if item.metadata.get("peer_wait", {}).get("kind") == "meeting"
            ]
            if waiting_for_meeting:
                resolved = self.communication.auto_resolve_stale_meetings(waiting_for_meeting)
                if resolved:
                    logger.info("Auto-resolved %d stalled meetings", len(resolved))
                    for item in waiting_for_meeting:
                        if item.id in resolved:
                            self.store.save(item)

        agents = self.org_engine.list_agents()
        now = datetime.now(UTC)

        for agent in agents:
            role_id = agent.role_id
            if self.heartbeat_role_ids is not None and role_id not in self.heartbeat_role_ids:
                continue
            if self._has_active_run(role_id):
                continue
            if len(self._active_runs) >= self.max_concurrent_runs:
                break
            last = self._last_heartbeat.get(role_id)
            if last and (now - last) < timedelta(seconds=self.interval_sec):
                continue
            logger.debug("Heartbeat tick: scheduling agent=%s", role_id)
            self._active_runs[role_id] = asyncio.create_task(self._run_agent_heartbeat(role_id))

    async def _run_agent_heartbeat(self, agent_id: str) -> None:
        """单次心跳周期：找该角色名下的待办卡，claim 后执行。

        ``_active_item_ids`` 在 run 期间标记当前 work item，防止未分配
        （assignee_role=None）卡被多个角色心跳并发重复执行。
        """
        try:
            candidate = None
            for item in self.store.list_all():
                if item.phase not in (Phase.QUEUED, Phase.READY):
                    continue
                if item.id in self._active_item_ids:
                    continue
                if item.assignee_role in (agent_id, None):
                    candidate = item
                    break
            if candidate is None:
                return
            self._last_heartbeat[agent_id] = datetime.now(UTC)
            if not self.dispatcher.claim(agent_id):
                logger.debug("Agent %s failed to claim seat, skipping", agent_id)
                return
            logger.info("Agent %s executing work item %s via heartbeat", agent_id, candidate.id)
            self._active_item_ids.add(candidate.id)
            try:
                result = await self.dispatcher.run_work_item(candidate.id)
                if getattr(result, "success", True) is False:
                    logger.warning(
                        "Agent %s heartbeat run for %s returned failure: %s",
                        agent_id,
                        candidate.id,
                        getattr(result, "output", "") or "",
                    )
            finally:
                self._active_item_ids.discard(candidate.id)
                self.dispatcher.release(agent_id)
        except Exception:
            logger.exception("Heartbeat run failed for agent=%s", agent_id)

    def _cleanup_done_runs(self) -> None:
        done = [k for k, v in self._active_runs.items() if v.done()]
        for key in done:
            task = self._active_runs.pop(key)
            if task.exception():
                logger.warning("Heartbeat run for %s ended with error: %s", key, task.exception())
