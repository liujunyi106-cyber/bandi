# 功能：Phase 转换钩子系统 — 5 活跃钩子 + 串行队列协调 + 停滞检测

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bandi.company.phase import Phase, is_terminal

logger = logging.getLogger(__name__)

STUCK_QUEUE_DEPTH_THRESHOLD = 5
STUCK_FOCUS_MINUTES = 10
_DONE_IDS_LIMIT = 1000
_RUNNABLE_PHASES = frozenset({Phase.READY, Phase.READY_FOR_REWORK})
_RUNNING_PHASE = frozenset({Phase.RUNNING})


@dataclass
class HookContext:
    work_item_id: str
    source_phase: Phase
    target_phase: Phase
    reason: str
    timestamp: datetime
    extra: dict[str, Any] | None = None


HookFn = Callable[[HookContext], None]

# -- Serial queue state --
_session_focus: dict[str, str] = {}
_focus_start: dict[str, datetime] = {}
_done_ids: set[str] = set()
_serial_queues: dict[str, list[str]] = {}
_DISPATCHER_WAKE_HOOKS: list[Callable[[], None]] = []
_STATE_LOCK = threading.Lock()


def register_dispatcher_wake(callback: Callable[[], None]) -> None:
    """CompanyDispatcher 构造时注册自身 signal_dispatcher_wake，供 hook 无环唤醒。"""
    with _STATE_LOCK:
        if callback not in _DISPATCHER_WAKE_HOOKS:
            _DISPATCHER_WAKE_HOOKS.append(callback)


def unregister_dispatcher_wake(callback: Callable[[], None]) -> None:
    """dispatcher shutdown 时移除唤醒回调。"""

    with _STATE_LOCK:
        try:
            _DISPATCHER_WAKE_HOOKS.remove(callback)
        except ValueError:
            pass


def get_session_focus() -> dict[str, str]:
    with _STATE_LOCK:
        return dict(_session_focus)


# 预留：串行队列只读访问器；启用时机 队列管理面
def get_serial_queue(role_key: str) -> list[str]:
    with _STATE_LOCK:
        return list(_serial_queues.get(role_key, []))


def clear_session_focus(session_id: str) -> None:
    with _STATE_LOCK:
        _session_focus.pop(session_id, None)
        _focus_start.pop(session_id, None)


def set_session_focus(session_id: str, work_item_id: str) -> None:
    with _STATE_LOCK:
        _session_focus[session_id] = work_item_id
        _focus_start[session_id] = datetime.now(UTC)


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFn]] = {}

    def register(self, group: str, hook: HookFn) -> None:
        if group not in self._hooks:
            self._hooks[group] = []
        self._hooks[group].append(hook)

    def unregister(self, group: str, hook: HookFn) -> None:
        if group not in self._hooks:
            return
        self._hooks[group] = [h for h in self._hooks[group] if h is not hook]

    def execute(self, group: str, ctx: HookContext) -> None:
        for hook in self._hooks.get(group, []):
            try:
                hook(ctx)
            except Exception:
                logger.exception("Hook %s failed for group %s", hook.__name__, group)


# -- Hook implementations --


def sync_task_status_hook(ctx: HookContext) -> None:
    if is_terminal(ctx.target_phase):
        logger.info(
            "WorkItem %s completed: %s -> %s (%s)",
            ctx.work_item_id,
            ctx.source_phase.value,
            ctx.target_phase.value,
            ctx.reason,
        )
    elif ctx.source_phase != ctx.target_phase:
        logger.debug(
            "WorkItem %s transition: %s -> %s",
            ctx.work_item_id,
            ctx.source_phase.value,
            ctx.target_phase.value,
        )


def signal_dispatcher_hook(ctx: HookContext) -> None:
    in_runnable = ctx.target_phase in _RUNNABLE_PHASES
    in_done = is_terminal(ctx.target_phase)
    in_running = ctx.target_phase in _RUNNING_PHASE
    if in_runnable or in_done or in_running:
        with _STATE_LOCK:
            wakes = list(_DISPATCHER_WAKE_HOOKS)
        for wake in wakes:
            try:
                wake()
            except Exception:
                logger.exception("dispatcher wake callback failed")


def refresh_dependents_hook(ctx: HookContext) -> None:
    if is_terminal(ctx.target_phase):
        logger.debug("WorkItem %s done, dependents may be refreshable", ctx.work_item_id)
    elif ctx.target_phase == Phase.AWAITING_HUMAN or ctx.target_phase == Phase.READY_FOR_REWORK:
        logger.debug("WorkItem %s needs attention, checking dependents", ctx.work_item_id)


def clear_session_focus_on_terminal_hook(ctx: HookContext) -> None:
    if not is_terminal(ctx.target_phase):
        return
    with _STATE_LOCK:
        _done_ids.add(ctx.work_item_id)
        if len(_done_ids) > _DONE_IDS_LIMIT:
            _done_ids.clear()
        session_id = ctx.extra.get("session_id", "") if ctx.extra else ""
        if not session_id:
            for sid, wiid in list(_session_focus.items()):
                if wiid == ctx.work_item_id:
                    session_id = sid
                    break
        if session_id:
            _session_focus.pop(session_id, None)
            _focus_start.pop(session_id, None)
            logger.debug(
                "Cleared session focus: %s (WorkItem %s done)", session_id, ctx.work_item_id
            )
        role_key = ctx.extra.get("role_key", "") if ctx.extra else ""
        if role_key and role_key in _serial_queues:
            _serial_queues[role_key] = [
                wiid for wiid in _serial_queues[role_key] if wiid != ctx.work_item_id
            ]
            if _serial_queues[role_key]:
                logger.debug("Role %s next in queue: %s", role_key, _serial_queues[role_key][0])


def enqueue_session_work_on_runnable_hook(ctx: HookContext) -> None:
    if ctx.target_phase not in _RUNNABLE_PHASES:
        return
    session_id = ctx.extra.get("session_id", "") if ctx.extra else ""
    if not session_id:
        return
    with _STATE_LOCK:
        if session_id in _session_focus:
            role_key = ctx.extra.get("role_key", "") if ctx.extra else ""
            if role_key:
                if role_key not in _serial_queues:
                    _serial_queues[role_key] = []
                if ctx.work_item_id not in _serial_queues[role_key]:
                    _serial_queues[role_key].append(ctx.work_item_id)
                    queue_depth = len(_serial_queues[role_key])
                    if queue_depth >= STUCK_QUEUE_DEPTH_THRESHOLD:
                        logger.warning(
                            "Role %s queue depth %d (threshold %d)",
                            role_key,
                            queue_depth,
                            STUCK_QUEUE_DEPTH_THRESHOLD,
                        )
        else:
            _session_focus[session_id] = ctx.work_item_id
            _focus_start[session_id] = datetime.now(UTC)


# -- Queue reconciliation --


# 预留：串行队列对账（done 集与队列交叉校正）；启用时机 队列管理面
def reconcile_role_serial_queues() -> dict[str, list[str]]:
    with _STATE_LOCK:
        done_ids = set(_done_ids)
        cleaned: dict[str, list[str]] = {}
        for role_key, queue in _serial_queues.items():
            active = [wiid for wiid in queue if wiid not in done_ids]
            if active and active != queue:
                logger.debug(
                    "Reconciled queue for %s: removed %d done items",
                    role_key,
                    len(queue) - len(active),
                )
            cleaned[role_key] = active
        _serial_queues.clear()
        _serial_queues.update(cleaned)
        return dict(_serial_queues)


def _get_done_ids() -> set[str]:
    with _STATE_LOCK:
        return set(_done_ids)


def check_stuck_focused_sessions() -> list[tuple[str, str, float]]:
    stuck: list[tuple[str, str, float]] = []
    now = datetime.now(UTC)
    with _STATE_LOCK:
        for session_id, work_item_id in list(_session_focus.items()):
            start = _focus_start.get(session_id)
            if start is None:
                continue
            elapsed = (now - start).total_seconds() / 60
            if elapsed >= STUCK_FOCUS_MINUTES:
                stuck.append((session_id, work_item_id, elapsed))
                logger.warning(
                    "Session %s stuck on WorkItem %s for %.1f minutes",
                    session_id,
                    work_item_id,
                    elapsed,
                )
    return stuck
