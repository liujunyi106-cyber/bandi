# 功能：会话重激活清扫——未读邮件触发工作项重开

"""后台清扫器：当已完成工作项的角色收到新的 actionable 邮件时重新激活。

（company_mode.py:9439）：周期扫描已完成工作项，角色收到未读 blocking 邮件时
真实重开（-> READY），调度器随后重新拾取。

- 状态机出口：``Phase.`` 增加唯一出边 -> READY（phase.py）
  供本模块守卫路径使用；``is_terminal`` 语义不变。
  ② ``comms_reactivation_depth`` 硬上限；③ 跨角色 (sender, subject) ping-pong。
- 真实重开：``transition_work_item`` 以 ``reactivated_by_inbound_comms`` 转 READY
  写 depth/fingerprint/cross_role_history 元数据。
- 接线（可选注入）：``dispatcher`` 广播 wake + ``heartbeat`` 立即唤醒角色。

project_id 非空时扫描全表 卡。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bandi.company._utils import as_int as _as_int
from bandi.company.comms import CommsMessage
from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher
from bandi.company.heartbeat import HeartbeatScheduler, run_tick_loop
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import REACTIVATION_REASON, WorkItem, transition_work_item

logger = logging.getLogger(__name__)

COMMS_REACTIVATION_MAX_DEPTH = 5
COMMS_CROSS_ROLE_HISTORY_MAX = 20


def _subject_hash(subject: str) -> str:
    """稳定主题指纹：小写 + SHA-1，用于跨角色 ping-pong 检测。"""
    return hashlib.sha1(subject.strip().lower().encode("utf-8")).hexdigest()


def _reactivation_blocked_by_history(
    item: WorkItem,
    actionable: list[CommsMessage],
    fingerprint: list[str],
) -> str | None:
    """

    返回跳过原因（非空=应跳过），None 表示允许重开。
    """
    metadata = item.metadata
    if metadata.get("comms_last_reactivation_fingerprint") == fingerprint:
        return "same_unread_fingerprint"
    if _as_int(metadata.get("comms_reactivation_depth")) >= COMMS_REACTIVATION_MAX_DEPTH:
        return "max_depth"
    history = list(metadata.get("comms_cross_role_history", []) or [])
    for msg in actionable:
        sender = msg.sender_role or ""
        if not sender:
            continue
        subject_hash = _subject_hash(msg.subject or "")
        for entry in history:
            if (
                entry.get("from_role") == sender
                and entry.get("subject_hash") == subject_hash
                and entry.get("target_work_item_id") == str(item.id)
            ):
                return "cross_role_ping_pong"
    return None


class CommsReactivationSweeper:
    """周期扫描 工作项，角色收到新 actionable mail 时触发重新激活。"""

    def __init__(
        self,
        *,
        store: WorkItemStore | None,
        project_id_getter: Callable[[], str | None],
        reactivate_fn: Callable[[WorkItem], Awaitable[bool]],
        interval_sec: float = 10.0,
        dispatcher: CompanyDispatcher | None = None,
        heartbeat: HeartbeatScheduler | None = None,
    ) -> None:
        self.store = store
        self.project_id_getter = project_id_getter
        self.reactivate_fn = reactivate_fn
        self.interval_sec = max(1.0, float(interval_sec))
        self.dispatcher = dispatcher
        self.heartbeat = heartbeat
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.last_reactivation_report: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if self.store is None:
            logger.debug("CommsReactivationSweeper: no store, skipping start")
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("CommsReactivationSweeper started (interval=%ss)", self.interval_sec)

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("CommsReactivationSweeper stop error")
        self._task = None
        logger.info("CommsReactivationSweeper stopped")

    # -- main loop ---------------------------------------------------------

    async def _tick_loop(self) -> None:
        await run_tick_loop(
            running=lambda: self._running,
            tick=self._tick,
            wait=lambda: asyncio.sleep(self.interval_sec),
            loop_name="CommsReactivationSweeper",
            loop_logger=logger,
        )

    async def _tick(self) -> None:
        project_id = (self.project_id_getter() or "").strip()
        if not project_id:
            return
        if self.store is None:
            return
        if getattr(self.store, "_conn", None) is None:
            # 不静默跳过——store 未持有连接时告警，避免私有字段重构后失效难排查
            logger.warning(
                "CommsReactivationSweeper: store has no active connection, sweep skipped"
            )
            return
        report: list[dict[str, Any]] = []
        woken_roles: set[str] = set()
        for item in self.store.list_by_phase(Phase.APPROVED):
            try:
                reactivated = await self.reactivate_fn(item)
            except Exception:
                logger.exception(
                    "CommsReactivationSweeper: reactivate_fn raised for item=%s",
                    item.id,
                )
                continue
            if reactivated:
                report.append(
                    {
                        "id": item.id,
                        "role": item.assignee_role or "",
                        "unread_msg_ids": list(
                            item.metadata.get("reactivation_unread_msg_ids", [])
                        ),
                    }
                )
                if item.assignee_role:
                    woken_roles.add(item.assignee_role)
        self.last_reactivation_report = report
        if report:
            for role in woken_roles:
                if self.heartbeat is not None:
                    self.heartbeat.wakeup(role, reason=REACTIVATION_REASON)
            if self.dispatcher is not None:
                self.dispatcher.signal_dispatcher_wake()
            logger.debug(
                "CommsReactivationSweeper: tick reactivated %d item(s) for project=%s",
                len(report),
                project_id,
            )


def make_unread_mail_reactivate_fn(
    communication: CommunicationManager,
    store: WorkItemStore,
) -> Callable[[WorkItem], Awaitable[bool]]:
    """构造完整版 reactivate_fn：角色有未读 blocking 邮件时真实重开工作项。

    三重防环守卫通过后，以 ``reactivated_by_inbound_comms`` 将工作项从
    转回 READY（调度池），并写元数据：
    - ``comms_reactivation_depth``: 重开次数（硬上限见模块常量）
    - ``comms_last_reactivation_fingerprint``: 本次未读 msg id 列表
    - ``comms_cross_role_history``: 跨角色 (sender, subject) 防 ping-pong
    - ``reactivation_pending`` / ``reactivation_unread_msg_ids``: 报告字段
    """

    async def reactivate(item: WorkItem) -> bool:
        role = item.assignee_role
        if not role:
            return False
        unread = communication.read_inbox(role, include_seen=False)
        actionable = [m for m in unread if m.blocking]
        if not actionable:
            return False
        fingerprint = [m.id for m in actionable]
        blocked = _reactivation_blocked_by_history(item, actionable, fingerprint)
        if blocked is not None:
            logger.debug("skip reactivation of item=%s: %s", item.id, blocked)
            return False
        depth = _as_int(item.metadata.get("comms_reactivation_depth")) + 1
        history = list(item.metadata.get("comms_cross_role_history", []) or [])
        for msg in actionable:
            sender = msg.sender_role or ""
            if not sender:
                continue
            history.append(
                {
                    "from_role": sender,
                    "subject_hash": _subject_hash(msg.subject or ""),
                    "target_work_item_id": str(item.id),
                    "msg_id": msg.id,
                    "depth": depth,
                }
            )
        original_metadata = dict(item.metadata)
        original_phase = item.phase
        try:
            item.metadata["comms_reactivation_depth"] = depth
            item.metadata["comms_last_reactivation_fingerprint"] = fingerprint
            item.metadata["comms_cross_role_history"] = history[-COMMS_CROSS_ROLE_HISTORY_MAX:]
            item.metadata["reactivation_pending"] = True
            item.metadata["reactivation_unread_msg_ids"] = fingerprint
            transition_work_item(item, Phase.READY, store, reason=REACTIVATION_REASON)
        except Exception:
            logger.warning(
                "reactivate item=%s failed to transition; rolling back metadata + phase",
                item.id,
                exc_info=True,
            )
            item.metadata = original_metadata
            item.phase = original_phase
            return False
        return True

    return reactivate
