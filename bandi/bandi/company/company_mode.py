# 功能：Company Mode 调度器——tick 循环 + 席位 claim/release + gate/review 处理 + runtime 接线
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bandi.company.approval import ApprovalEngine
from bandi.company.communication import CommunicationManager
from bandi.company.dispatch_guard import DispatchGuardVerdict, evaluate_dispatch_guard
from bandi.company.employee_evolution import EmployeeEvolutionManager
from bandi.company.gate_harness import GateHarness, GateResult, GateVerdict
from bandi.company.group_chat_protocol import is_pass_output
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import (
    InvalidPhaseTransitionError,
    Phase,
    TurnMode,
    coerce_phase,
    is_terminal,
    kanban_column,
)
from bandi.company.phase_hooks import (
    HookContext,
    HookRegistry,
    clear_session_focus_on_terminal_hook,
    enqueue_session_work_on_runnable_hook,
    refresh_dependents_hook,
    register_dispatcher_wake,
    signal_dispatcher_hook,
    sync_task_status_hook,
    unregister_dispatcher_wake,
)
from bandi.company.recruiter import Recruiter, RecruitStatus
from bandi.company.reorg_manager import ReorgChangeSet, ReorgProposal, ReorgScope
from bandi.company.review_bridge import build_review_card, parse_review_verdict
from bandi.company.seat_executor import SOP_PROGRESS_KEY, SeatExecutor, SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.task_preparer import TaskPreparer
from bandi.company.turn_mode import infer_turn_mode
from bandi.company.work_item import (
    REACTIVATION_REASON,
    WorkItem,
    WorkItemTurnType,
    create_work_item,
    transition_work_item,
)
from bandi.company.work_item_transition import (
    DELEGATED_CHILDREN_PENDING_KEY,
    DEPENDENCY_WORK_ITEM_IDS_KEY,
    FRONTIER_KEY,
    WAITING_FOR_CHILDREN_FRONTIER,
    has_pending_settlement_release,
    refresh_dependents,
    work_item_id,
)
from bandi.core.abort_controller import AbortController

META_RUNTIME_OWNER = "runtime_owner"
DEFAULT_RUNTIME_OWNER = "company"

if TYPE_CHECKING:
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.heartbeat import HeartbeatScheduler
    from bandi.company.reactivation_sweeper import CommsReactivationSweeper
    from bandi.company.reorg_manager import ReorgManager

# runtime_metadata 键常量化（gate 评估 / todo 执行 / run_work_item 三处共享）

logger = logging.getLogger(__name__)


def _as_bool(value: Any, default: bool = False) -> bool:
    """metadata 布尔容错解析——API/JSON 层布尔可能被序列化为字符串。

    bool("false") is True 是经典陷阱：字符串按语义解析（true/1/yes/on）
    其余类型回退 bool(value)。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value) if value is not None else default


def _apply_and_save_checked(
    store: WorkItemStore, item: WorkItem, apply: Callable[[WorkItem], None]
) -> WorkItem:
    """功能：CAS 保存 + 冲突重放——save_checked 失败（revision 冲突=他进程已写）时
    重读最新快照并重新应用本进程字段变更后重试一次；仍失败记 warning 且不回写旧快照
    （防覆盖他进程数据，变更留在内存由下游 tick/recover 兜底）。返回最新 item。"""
    apply(item)
    if store.save_checked(item, item.revision):
        return item
    fresh = store.get(item.id)
    if fresh is None:
        return item
    apply(fresh)
    if not store.save_checked(fresh, fresh.revision):
        logger.warning(
            "work item %s concurrent save conflict persists; change kept in memory only",
            item.id,
        )
    return fresh


# sweeper 最小扫描间隔（秒）——API 高频 tick 时防全表扫描 × inbox IO 放大
_SWEEP_MIN_INTERVAL = 10.0

META_LAST_OUTPUT = "_last_output"
META_TASK_STATUS = "_task_status"
META_RISKS = "_risks"

_DISPATCH_GUARD_VIOLATION = (
    "the previous DELEGATE turn produced no board change (no child WorkItem created, "
    "no existing child modified/deleted) and no NO_DELEGATION_JUSTIFICATION line"
)


def _artifact_guard_missing(item: WorkItem) -> list[str]:
    """功能：任务声明 expected_artifacts 时校验产物可读回（ArtifactGuard 生产接入）；
    未声明/无项目根一律返回空列表（守卫不误伤）。"""
    metadata = item.metadata or {}
    expected = metadata.get("expected_artifacts")
    if not isinstance(expected, list):
        return []
    paths = [str(path).strip() for path in expected if str(path).strip()]
    if not paths:
        return []
    project_id = str(metadata.get("project_id") or "").strip()
    if not project_id:
        return []
    from bandi.company.seat_executor import _resolve_project_root_for

    root = _resolve_project_root_for(project_id)
    if not root:
        return []
    from bandi.agent.artifact_guard import ArtifactGuard

    return ArtifactGuard(root).missing(paths)


@dataclass
class DispatcherConfig:
    # 同时执行的工作项上限（卡级接续：任一完成即补位）
    max_concurrent: int = 3
    # 自动建链：顶层计划卡完成后系统按白名单建串行子卡（模型不派发时兜底）
    serial_chain: bool = True
    enable_auto_process: bool = True
    # review 侧 rework 上限——达成时强制 auto_done，防无限循环
    max_review_reworks: int = 5
    # 宽松自动化：员工卡完成提交后停在经理评审且无评审意见时自动通过放行
    # （人无需逐卡点批；需强审的卡仍可显式写 review_verdict）；关则保持人工评审
    auto_approve_reviews: bool = True
    # 会整批 cancel，是明确要避开的反例。
    # 等待用户授权由审批窗口（30 分钟 TTL）承担——超时执行被取消、降级文本、释放槽位
    per_item_timeout_sec: float = 1860.0
    # 单次 tick 的补位预算（秒；<=0 不限）：超时后不再派新卡，在跑卡继续收尾
    tick_budget_sec: float = 300.0
    max_items_per_tick: int = 64


@dataclass
class KanbanSnapshot:
    """查询快照（区别于 tick 报告）：仅承载 kanban 列分组计数。"""

    captured_at: datetime
    column_counts: dict[str, int]


@dataclass
class DispatcherReport:
    tick_id: str
    started_at: datetime
    processed: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    transitions: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    gate_results: list[tuple[str, str]] = field(default_factory=list)
    metadata: dict[str, int] = field(default_factory=dict)
    # 团队看板展示「谁跑了多久/并行区间」的数据源（transitions 只管相位迁移）
    timeline: list[tuple[str, str, float, bool]] = field(default_factory=list)

    def elapsed(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def has_errors(self) -> bool:
        return len(self.errors) > 0


MAX_RECOVERY_COUNT = 3

# 员工持久历史空间键——同一 (project, team, role, employee) 的任务
# 共享一个会话（「员工记得上次的事」），但只影响会话拼接：调度队列 frontier tag
# 仍按 session_scope_id（canonical 的 `scoped_queue_key` 不消费本键）。
_COMPANY_HISTORY_SCOPE = "staff"

# 报告会撑爆 API 响应与前端渲染（todo/rework 同口径）
_REPORT_ERROR_MAX_CHARS = 2000


def _clip_report_error(message: str) -> str:
    # 功能：报告错误条目有界化（超限截断 + 省略号）
    if len(message) <= _REPORT_ERROR_MAX_CHARS:
        return message
    return message[:_REPORT_ERROR_MAX_CHARS] + "…"


def _is_deliverable_output(result: SeatResult) -> bool:
    # 功能：判定 SeatResult 产出可否写入用户可见链路（线程 / 会话历史 / _last_output）。
    # 与 AWAITING_APPROVAL 下以 `{` 开头的问题卡 JSON（旧路径/异常路径）一律不写；
    # 合法业务 JSON 产出不拦（不一刀切封 `{`——只认审批协议形态）。
    reason = str((result.metadata or {}).get("terminal_reason") or "")
    texts = (
        result.output or "",
        str((result.metadata or {}).get("_original_output") or ""),
    )
    for text in texts:
        stripped = text.lstrip()
        if stripped.startswith("APPROVAL_REQUIRED:"):
            return False
        if reason == "AWAITING_APPROVAL" and stripped.startswith("{"):
            return False
    return True


def _seat_task_status(result: SeatResult) -> str:
    # 功能：员工卡任务状态三态口径——审批等待落第三态 awaiting_approval
    reason = str((result.metadata or {}).get("terminal_reason") or "")
    if reason == "AWAITING_APPROVAL":
        return "awaiting_approval"
    return "completed" if result.success else "failed"


def _latest_pending_ref(session_id: str) -> tuple[str, str] | None:
    # 功能：取该会话最近一条 pending 的 (tool_call_id, tool_name)，供卡级审批回填
    try:
        from bandi.tools.approval import get_approval_store

        pendings = get_approval_store().list_pending(session_id)
    except Exception:  # noqa: BLE001 — 审批元数据只是恢复精度优化，不阻断落账
        return None
    if not pendings:
        return None
    latest = pendings[-1]
    return str(latest.tool_call_id or ""), str(latest.tool_name or "")


def _approval_ref_for_result(result: SeatResult, session_id: str) -> tuple[str, str]:
    # 功能：审批锚点优先取执行链上报值，缺失才回退「查本会话最新 pending」
    meta = result.metadata or {}
    call_id = str(meta.get("approval_tool_call_id") or "").strip()
    tool_name = str(meta.get("approval_tool_name") or "").strip()
    if call_id:
        return call_id, tool_name
    pending = _latest_pending_ref(session_id)
    if pending is None:
        return "", tool_name
    return pending[0], (tool_name or pending[1])


def resolve_thread_manager_for(
    item: WorkItem,
    *,
    default: Any | None,
    resolver: Any | None = None,
) -> Any | None:
    """功能：产出回写线程管理器路由——卡 metadata.project_id 非 company 且有
    resolver 时解析项目线程管理器；resolver 缺失/失败/返回空一律回落 default（不阻断执行）。"""
    project_id = str((getattr(item, "metadata", None) or {}).get("project_id") or "").strip()
    if not project_id or project_id == "company" or resolver is None:
        return default
    try:
        resolved = resolver(project_id)
    except Exception:  # noqa: BLE001 — resolver 失败回落 default，线程回写不阻断执行
        return default
    return resolved if resolved is not None else default


def _auto_chain_title(plan_brief: str, role_name: str, role_id: str) -> str:
    """功能：自动串行链子卡标题——取上游计划「分工与执行链」中该角色的交付物；
    未命中用中性兜底（不得把岗位职责提示词当任务标题）。"""
    names = {value for value in (role_name, role_id) if value}
    for line in (plan_brief or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or not any(name in cells[2] for name in names):
            continue
        title = cells[1].strip()
        if title and set(title) - set("-: "):
            return title[:40]
    for line in (plan_brief or "").splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        for name in names:
            if stripped.startswith(f"@{name}"):
                text = stripped.split("：", 1)[-1].strip() if "：" in stripped else stripped
                if text:
                    return text[:40]
    return f"{role_name}：本环节交付"


class CompanyDispatcher:
    def __init__(
        self,
        store: WorkItemStore,
        recruiter: Recruiter,
        org_engine: OrgEngine,
        gate_harness: GateHarness | None = None,
        approval_engine: ApprovalEngine | None = None,
        seat_executor: SeatExecutor | None = None,
        external_seat_executor: SeatExecutor | None = None,
        communication: CommunicationManager | None = None,
        hook_registry: HookRegistry | None = None,
        config: DispatcherConfig | None = None,
        task_preparer: TaskPreparer | None = None,
        runtime: CompanyRuntime | None = None,
        reorg_manager: ReorgManager | None = None,
        employee_evolution: EmployeeEvolutionManager | None = None,
        thread_manager: Any | None = None,
        runtime_owner: str = DEFAULT_RUNTIME_OWNER,
    ) -> None:
        self.store = store
        self.recruiter = recruiter
        self.org_engine = org_engine
        # 供其他进程的 recover_stale_claims 判定存活（存活证明在共享
        # 存储而非进程内存，防 CLI/API 双进程互相误恢复）
        self.instance_id = uuid.uuid4().hex
        self.gate_harness = gate_harness or GateHarness()
        # custom_runtime）均未显式传参导致 TaskPreparer/ApprovalEngine 生产
        # 零实例化：reorg 审批（reorg_manager:164）在需确认时抛 RuntimeError
        # run_work_item 的 task_preparer.prepare（857）恒不触发。与
        if approval_engine is None:
            approval_engine = ApprovalEngine()
        self.approval_engine = approval_engine
        self.seat_executor = seat_executor
        # （路线 ）：外部席位执行器——runtime_metadata 携带
        # assigned_external_agent（非 force_native）时 run_work_item 改走此执行器；
        self.external_seat_executor = external_seat_executor
        self.communication = communication
        self.hook_registry = hook_registry or self._build_default_hook_registry()
        self.config = config or DispatcherConfig()
        if task_preparer is None:
            task_preparer = TaskPreparer()
        self.task_preparer = task_preparer
        self._runtime = runtime
        self._reorg_manager = reorg_manager
        # （home 从 store 数据目录推导，避免散落用户主目录）
        if employee_evolution is None:
            employee_evolution = EmployeeEvolutionManager(
                home_dir=Path(getattr(store, "db_path", "")).resolve().parent
            )
        self.employee_evolution = employee_evolution
        self.threads = thread_manager
        # 多 dispatcher 共享 store 时的归属：主公司默认 company，自定义 runtime 用 custom:<org>。
        self.runtime_owner = str(runtime_owner or DEFAULT_RUNTIME_OWNER)
        # 群聊回合服务——API 装配注入；未装配时群聊仍按单卡/旧路径执行
        self.group_rounds: Any | None = None
        # 项目线程回写路由解析器——app.py 装配注入 _get_project_threads
        self.thread_manager_resolver: Any | None = None
        # `session_writeback.write_employee_reply_to_session` 的绑定闭包）——
        self.session_writer: Any | None = None
        self._wake_event = asyncio.Event()
        self._active_claims: dict[str, str] = {}
        # 本集合为空 → tick 可识别「phase 停在 in-flight 但无活跃执行」的遗留卡
        self._in_flight_work_item_ids: set[str] = set()
        self._in_flight_lock = threading.Lock()
        # 构造即注册 wake 回调，使 phase 转换 hook（signal_dispatcher_hook）能无环唤醒本 dispatcher。
        register_dispatcher_wake(self.signal_dispatcher_wake)
        # sweeper 实例缓存 + 扫描时间戳（tick 节流防 IO 放大）
        self._cached_sweeper: Any | None = None
        self._last_sweep_at: float = 0.0
        self._heartbeat: HeartbeatScheduler | None = None
        self._persisted_meeting_upgrades: set[tuple[str, str]] = set()
        # 跨进程并发仍由 execution_claim CAS 兜底
        self._tick_lock = asyncio.Lock()
        # native 执行器经 bind_abort_provider 透传 controller，external 经 interrupt() 树杀
        self._work_item_aborts: dict[str, AbortController] = {}
        # 执行协程句柄：abort 时主动 cancel，防止卡在不可轮询的 await（工具/审批/流式）
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_executions: dict[str, tuple[Any, Any]] = {}
        for _executor in (self.seat_executor, self.external_seat_executor):
            _bind = getattr(_executor, "bind_abort_provider", None)
            if _bind is not None:
                _bind(self._work_item_aborts.get)

    def _role_display_name(self, role_id: str) -> str:
        """功能：角色显示名（回写会话历史时快照；查不到返回空串，不阻断回写）。"""
        if not role_id:
            return ""
        org = getattr(self, "org_engine", None)
        if org is None:
            return ""
        try:
            role = org.get_role(role_id)
        except Exception:  # noqa: BLE001 — 组织查询失败不阻断产出回写
            return ""
        return str(getattr(role, "name", "") or "") if role is not None else ""

    async def interrupt_work_item(self, work_item_id: str) -> bool:
        """功能：中断执行中的工作项（用户中断公司任务）——置位 AbortController、
        取消执行协程并强制落终态/释放租约；无活动执行返回 False。"""
        controller = self._work_item_aborts.get(work_item_id)
        if controller is None:
            return False
        controller.abort("user_interrupt")
        active = self._active_executions.get(work_item_id)
        if active is not None:
            executor, runtime = active
            interrupt_fn = getattr(executor, "interrupt", None)
            if interrupt_fn is not None:
                try:
                    await interrupt_fn(runtime)
                except Exception:
                    logger.debug("executor interrupt failed", exc_info=True)
            cleanup = getattr(self, "employee_interaction_cleanup", None)
            if cleanup is not None:
                targets = {runtime.member_session_id()}
                scope_id = str(getattr(runtime, "session_scope_id", "") or "")
                if scope_id:
                    targets.add(scope_id)
                for target in targets:
                    try:
                        cleanup(target)
                    except Exception:
                        logger.debug("interaction cleanup failed", exc_info=True)
        # 只置位 controller 不够：执行可能卡在工具/审批/流式 await（不轮询 abort），
        # 必须取消执行协程；超时后仍强制清理，避免卡片永久 RUNNING + 租约永续。
        task = self._execution_tasks.get(work_item_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                logger.debug("execution task cancel failed", exc_info=True)
        self._mark_interrupted(work_item_id, "user_interrupt")
        if self.group_rounds is not None:
            try:
                self.group_rounds.on_slot_interrupted(work_item_id, "user_interrupt")
            except Exception:
                logger.debug("group round interrupt advance failed", exc_info=True)
        return True

    async def cancel_work_item(
        self, work_item_id: str, reason: str = "work_item_cancelled"
    ) -> bool:
        """功能：取消非终态工作项——运行中先中断，再统一落 CANCELLED 并释放席位。"""
        item = self.store.get(work_item_id)
        if item is None or is_terminal(item.phase):
            return False
        if item.phase == Phase.RUNNING or work_item_id in self._in_flight_work_item_ids:
            await self.interrupt_work_item(work_item_id)
            item = self.store.get(work_item_id) or item
        if is_terminal(item.phase):
            return True
        try:
            item = transition_work_item(item, Phase.CANCELLED, self.store, reason=reason)
        except InvalidPhaseTransitionError:
            logger.debug(
                "cancel work item %s failed: invalid transition from %s",
                work_item_id,
                item.phase,
            )
            return False
        if item.assignee_role:
            self.release(item.assignee_role, item.id)
        release_claim = getattr(self.store, "release_execution_claim", None)
        if callable(release_claim):
            release_claim(work_item_id, instance_id=self.instance_id)
        return True

    def _mark_interrupted(self, work_item_id: str, reason: str) -> SeatResult:
        """功能：中断收尾——仅对仍在 RUNNING 的卡落人工态并释放执行租约（幂等）。"""
        item = self.store.get(work_item_id)
        if item is not None and item.phase == Phase.RUNNING:

            def _apply(target: WorkItem) -> None:
                target.runtime_metadata[META_TASK_STATUS] = "interrupted"
                target.runtime_metadata["interrupted_reason"] = reason

            item = _apply_and_save_checked(self.store, item, _apply)
            if item.phase == Phase.RUNNING:
                item = transition_work_item(
                    item,
                    Phase.NEEDS_ATTENTION,
                    self.store,
                    reason="interrupted by user",
                )
        self.store.release_execution_claim(work_item_id, instance_id=self.instance_id)
        return SeatResult(
            success=False, output="interrupted by user", metadata={"interrupted": True}
        )

    @staticmethod
    def _build_default_hook_registry() -> HookRegistry:
        """默认 HookRegistry：dispatcher_tick 组注册 5 个内置 hook（CM-11）。"""
        registry = HookRegistry()
        for hook in (
            sync_task_status_hook,
            signal_dispatcher_hook,
            refresh_dependents_hook,
            clear_session_focus_on_terminal_hook,
            enqueue_session_work_on_runnable_hook,
        ):
            registry.register("dispatcher_tick", hook)
        return registry

    def shutdown(self) -> None:
        unregister_dispatcher_wake(self.signal_dispatcher_wake)

    def _build_heartbeat(self) -> HeartbeatScheduler:
        """

        惰性构建——claim/release 与 tick 走同一席位协议，不会双执行同一卡
        （_active_item_ids 防并发，见 test_heartbeat）。
        """
        if self._heartbeat is None:
            from bandi.company.heartbeat import HeartbeatScheduler

            self._heartbeat = HeartbeatScheduler(
                store=self.store,
                org_engine=self.org_engine,
                dispatcher=self,
                communication=self.communication,
            )
        return self._heartbeat

    def _build_reactivation_sweeper(self) -> CommsReactivationSweeper | None:
        """构造通信驱动的 重新激活 sweeper——卡收到
        新的 blocking 未读邮件后触发重开。dispatcher 持有 communication/
        store，具备接线条件时返回 sweeper（无 communication 时返回 None）。
        """
        from bandi.company.reactivation_sweeper import (
            CommsReactivationSweeper,
            make_unread_mail_reactivate_fn,
        )

        if self.communication is None:
            return None
        return CommsReactivationSweeper(
            store=self.store,
            project_id_getter=lambda: getattr(self.communication, "project_id", "default") or "",
            reactivate_fn=make_unread_mail_reactivate_fn(self.communication, self.store),
            dispatcher=self,
            heartbeat=self._build_heartbeat(),
        )

    async def run_forever(
        self,
        running: Callable[[], bool],
        *,
        wait_timeout_sec: float = 30.0,
        loop_name: str = "CompanyDispatcher",
    ) -> None:
        """wake 驱动的调度主循环：tick 消费 wake_event，无事可做时休眠。

        wake（转换/外部触发）后立即 tick，空闲时按超时周期轮询。
        同时启动通信驱动的重新激活 sweeper（卡收到新 blocking
        邮件时重开），主循环结束即停止。
        """
        from bandi.company.heartbeat import run_tick_loop

        sweeper = self._build_reactivation_sweeper()
        heartbeat = self._build_heartbeat()

        async def wait() -> None:
            await asyncio.wait_for(self._wake_event.wait(), timeout=wait_timeout_sec)
            self._wake_event.clear()

        try:
            if heartbeat is not None:
                await heartbeat.start()
            if sweeper is not None:
                await sweeper.start()
            await run_tick_loop(
                running=running,
                tick=self.tick,
                wait=wait,
                loop_name=loop_name,
                loop_logger=logger,
            )
        finally:
            if heartbeat is not None:
                await heartbeat.stop()
            if sweeper is not None:
                await sweeper.stop()

    @property
    def wake_event(self) -> asyncio.Event:
        return self._wake_event

    def signal_dispatcher_wake(self) -> None:
        self._wake_event.set()

    def _build_runtime(
        self,
        *,
        session_scope_id: str,
        role_id: str = "",
        employee_id: str = "",
        team_instance_id: str = "",
        project_id: str = "",
        history_scope_id: str = "",
    ) -> CompanyRuntime:
        from bandi.company.company_runtime import CompanyRuntime

        # 项目维度优先级：工作项自带项目归属 > 通信管理器默认（生产装配为 "company"）>
        # 同一个记忆桶（跨项目混桶）。
        runtime = CompanyRuntime(
            project_id=(
                str(project_id or "").strip()
                or (self.communication.project_id if self.communication else "")
                or "default"
            ),
            session_scope_id=session_scope_id,
            role_id=role_id,
            employee_id=employee_id,
            team_instance_id=team_instance_id,
            history_scope_id=history_scope_id,
            dependency_ready=lambda dep_id: (
                (dep := self.store.get(dep_id)) is None or is_terminal(dep.phase)
            ),
        )
        runtime.sync_home_teams_from_roles(self.org_engine.list_roles())
        return runtime

    def _home_team_for_role(self, role_id: str) -> str:
        """从组织架构查询角色归属团队（org 为单 org 权威，避免重复维护）。"""
        rid = (role_id or "").strip()
        if not rid:
            return ""
        role = self.org_engine.get_role(rid)
        return (role.team_id if role else "") or ""

    def _resolve_assignee_employee(self, role_id: str) -> Any:
        """功能：把角色 id 解析为该角色下的主责员工（org 权威，取 active 首位）。

        解析不到返回 None——调用方不得用 role_id 冒充 employee_id：seat 执行器按
        employee_id 查 llm_config，用 role_id 查恒为 None，会使 per-employee 模型路由
        （换大脑）静默退化为全局 default_model。
        """
        rid = (role_id or "").strip()
        if not rid:
            return None
        employees = self.org_engine.list_employees(role_id=rid)
        return employees[0] if employees else None

    def _resolve_assignee_employee_id(self, item: Any) -> str:
        """功能：解析卡的实际执行员工 id——metadata 指派优先（须真实存在于 org
        脏 id 不采用），否则按角色反查 active 首位；均无返回空串（不得回落 role_id
        冒充员工——经验/成本归属会挂错，per-employee 模型路由也会假启用）。"""
        existing = dict((item.metadata or {}).get("employee_assignment") or {})
        pinned = str(existing.get("employee_id") or "").strip()
        if pinned and self.org_engine.get_employee(pinned) is not None:
            return pinned
        employee = self._resolve_assignee_employee(item.assignee_role or "")
        return employee.employee_id if employee else ""

    @property
    def runtime(self) -> CompanyRuntime:
        """公司级调度运行时（惰性装配）：承载 role queues / member sessions / claimed 状态。"""
        if self._runtime is None:
            self._runtime = self._build_runtime(session_scope_id="company-runtime")
        return self._runtime

    # -- Reorg gateway（运行时组织重构入口） --

    @property
    def reorg_manager(self) -> ReorgManager:
        if self._reorg_manager is None:
            from bandi.company.reorg_manager import ReorgManager

            self._reorg_manager = ReorgManager(
                store=self.store,
                org_engine=self.org_engine,
                approval_engine=self.approval_engine,
                communication=self.communication,
            )
        return self._reorg_manager

    def propose_reorg(
        self,
        *,
        project_id: str,
        summary: str,
        rationale: str = "",
        title: str = "",
        initiated_by: str = "owner",
        source_role_id: str = "",
        changeset: ReorgChangeSet | dict[str, Any] | None = None,
        scope: ReorgScope | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReorgProposal:
        return self.reorg_manager.propose_reorg(
            project_id=project_id,
            summary=summary,
            rationale=rationale,
            title=title,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            changeset=changeset,
            scope=scope,
            session_id=session_id,
            task_id=task_id,
            metadata=metadata,
        )

    async def request_reorg_approval(self, proposal_id: str) -> tuple[bool, ReorgProposal]:
        return await self.reorg_manager.request_reorg_approval(proposal_id)

    def set_reorg_approval(
        self, proposal_id: str, approved: bool, notes: str = "", source: str = "manual"
    ) -> ReorgProposal:
        return self.reorg_manager.set_reorg_approval(
            proposal_id, approved, notes=notes, source=source
        )

    def review_work_item(
        self,
        work_item_id: str,
        verdict: str,
        feedback: str = "",
        reviewer: str = "manual",
    ) -> WorkItem | None:
        """评审决策写入面——写 review_verdict + structured_review_verdict 到 metadata。

        生产路径（CLI/API）据此解除 AWAITING_MANAGER_REVIEW / AWAITING_HUMAN 滞留。
        verdict 仅接受 approve / rework，其余抛 ValueError（防未知值静默滞留）。
        """
        if verdict not in ("approve", "rework"):
            raise ValueError(f"invalid review verdict {verdict!r} (approve|rework)")
        item = self.store.get(work_item_id)
        if item is None:
            raise KeyError(f"work item not found: {work_item_id}")
        # 仅评审态卡可消费 verdict——对 NEEDS_ATTENTION 等非评审态写 verdict
        # 会在 tick 消费时抛 InvalidPhaseTransitionError 中断整轮调度
        if item.phase not in (Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN):
            raise ValueError(f"review verdict only valid in review phases, got {item.phase.value}")
        metadata = dict(item.metadata or {})
        metadata["review_verdict"] = verdict
        metadata["structured_review_verdict"] = {
            "verdict": verdict,
            "reason": feedback,
            "reviewer": reviewer,
        }
        item.metadata = metadata
        self.store.save(item)
        # 长轮询 才消费（评审决策延迟生效）；wake 立即触发下次 tick
        self.signal_dispatcher_wake()
        return item

    def retry_work_item(self, work_item_id: str) -> WorkItem | None:
        """修复：NEEDS_ATTENTION 卡恢复路径——人工/API 重试转 READY 重新
        入队调度。NEEDS_ATTENTION→READY 无直达边，经合法两步（→RUNNING→READY
        phase.py:90/65）实现，等价重新入队。
        """
        item = self.store.get(work_item_id)
        if item is None:
            raise KeyError(f"work item not found: {work_item_id}")
        if item.phase != Phase.NEEDS_ATTENTION:
            raise ValueError(
                f"retry only valid for NEEDS_ATTENTION work items, got {item.phase.value}"
            )
        item = transition_work_item(
            item, Phase.RUNNING, self.store, reason="dispatcher: manual retry"
        )
        item = transition_work_item(
            item, Phase.READY, self.store, reason="dispatcher: manual retry re-queue"
        )
        self.signal_dispatcher_wake()
        return item

    def resume_after_approval(
        self,
        session_id: str,
        *,
        decision: str = "",
        tool_name: str = "",
        note: str = "",
        tool_call_id: str = "",
    ) -> int:
        """功能：审批决策后的卡恢复——把 `_approval_session_id == session_id`
        且 `_task_status == awaiting_approval` 的卡重新入队，并写入一次性
        `metadata["approval_resume"]` 指令（seat 执行轮前置注入 → 模型按
        「已批准/已拒绝」重发/放弃调用 → executor 消费 grant 放行）。

        精度：优先按 tool_call_id 精确命中该次审批；卡上没有记录时退回
        tool_name 匹配；都没有（旧数据）才退回会话级全量恢复。
        相位处理：决策先于下一 tick（卡仍在 RUNNING，执行已结束且租约已释放）→
        直接 RUNNING→READY；已转 NEEDS_ATTENTION（gate 连锁）→ 走 retry_work_item。
        返回命中恢复的卡数（0 = 无匹配；best-effort 调用方不因 0 报错）。
        """
        if not session_id:
            return 0
        resumed = 0
        logger.info(
            "approval resume requested: session=%s decision=%s tool=%s tool_call=%s",
            session_id,
            decision,
            tool_name,
            tool_call_id,
        )

        def _apply(target: WorkItem) -> None:
            target.metadata["approval_resume"] = {
                "decision": decision,
                "tool_name": tool_name,
                "note": note,
            }

        candidates: list[WorkItem] = []
        for item in self._owned_items():
            meta = item.runtime_metadata or {}
            if str(meta.get("_approval_session_id") or "") != str(session_id):
                continue
            if meta.get("_task_status") != "awaiting_approval":
                continue
            candidates.append(item)
        matched = candidates
        if tool_call_id:
            precise = [
                item
                for item in candidates
                if str((item.runtime_metadata or {}).get("_approval_tool_call_id") or "")
                == str(tool_call_id)
            ]
            if precise:
                matched = precise
        if matched is candidates and tool_name:
            named = [
                item
                for item in candidates
                if str((item.runtime_metadata or {}).get("_approval_tool_name") or "") == tool_name
            ]
            if named:
                matched = named
        candidates = matched

        for item in candidates:
            item = _apply_and_save_checked(self.store, item, _apply)
            if item.phase == Phase.NEEDS_ATTENTION:
                self.retry_work_item(item.id)
            elif item.phase == Phase.RUNNING:
                transition_work_item(
                    item, Phase.READY, self.store, reason="approval decision: re-queue"
                )
                self.signal_dispatcher_wake()
            else:
                # 已在队列（READY 等）——只需唤醒调度立刻看到
                self.signal_dispatcher_wake()
            resumed += 1
        logger.info("approval resume matched %d item(s) for session=%s", resumed, session_id)
        return resumed

    def apply_reorg(self, proposal_id: str) -> dict[str, Any]:
        return self.reorg_manager.apply_reorg(proposal_id)

    def suggest_task_adjustment(
        self,
        *,
        project_id: str,
        source_role_id: str,
        summary: str,
        changeset: ReorgChangeSet | dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return self.reorg_manager.suggest_task_adjustment(
            project_id=project_id,
            source_role_id=source_role_id,
            summary=summary,
            changeset=changeset,
            session_id=session_id,
            task_id=task_id,
        )

    # -- Runtime ownership --

    def _owns(self, item: WorkItem) -> bool:
        # 功能：旧卡无 owner 视为主公司，避免升级后存量卡失联。
        owner = str((item.metadata or {}).get(META_RUNTIME_OWNER) or DEFAULT_RUNTIME_OWNER)
        return owner == getattr(self, "runtime_owner", DEFAULT_RUNTIME_OWNER)

    def _owned_items(self) -> list[WorkItem]:
        # 功能：只返回本 dispatcher 归属的卡，防止主链与自定义 runtime 互抢。
        return [item for item in self.store.list_all() if self._owns(item)]

    # -- Seat management --

    def claim(self, role_id: str, work_item_id: str = "") -> bool:
        seat = self.recruiter.dispatch(role_id)
        if not seat:
            return False
        self._active_claims[role_id] = str(work_item_id or "")
        return True

    def release(self, role_id: str, work_item_id: str = "") -> bool:
        """功能：释放角色席位；指定 work_item_id 时只释放该卡持有的席位。"""
        if self.recruiter.get_seat(role_id) is None:
            return False
        owner = self._active_claims.get(role_id)
        if owner is None:
            return True
        if work_item_id and owner and owner != work_item_id:
            return False
        self._active_claims.pop(role_id, None)
        return self.recruiter.release(role_id)

    def release_all(self) -> None:
        for role_id in list(self._active_claims):
            self.release(role_id)

    def is_claimed(self, role_id: str) -> bool:
        seat = self.recruiter.get_seat(role_id)
        if seat is None:
            return False
        return seat.status in (RecruitStatus.RECRUITED, RecruitStatus.BUSY)

    def _tick_stuck_focus_check(self, report: DispatcherReport) -> None:
        """功能：停滞会话周期巡检——聚焦超时（≥ `STUCK_FOCUS_MINUTES`）逐条
        告警（日志 + 报告 errors），失败不阻断调度主链。"""
        from bandi.company.phase_hooks import check_stuck_focused_sessions

        try:
            stuck = check_stuck_focused_sessions()
        except Exception as e:  # noqa: BLE001 — 巡检失败不阻断调度主链
            logger.debug("stuck focus check failed: %s", e)
            return
        for stuck_session_id, stuck_work_item_id, elapsed in stuck:
            report.errors.append(
                _clip_report_error(
                    f"session {stuck_session_id} stuck on work item {stuck_work_item_id} "
                    f"for {elapsed:.1f} minutes"
                )
            )

    def _tick_deadlock_check(self, report: DispatcherReport) -> None:
        """单次 tick 死锁检测——从 store 组 WAITING_FOR_PEER 卡等待图
        检出互等环记入 report.errors（此前只在 run_forever heartbeat 触发
        生产零调用导致检测从未跑过）。"""
        if self.communication is None or not hasattr(self.communication, "build_waiting_graph"):
            return
        try:
            peer_items = [
                item for item in self._owned_items() if item.phase == Phase.WAITING_FOR_PEER
            ]
            graph = self.communication.build_waiting_graph(peer_items)
            if not graph:
                return
            cycles = self.communication.detect_deadlocks(graph)
            if cycles:
                cycle_desc = "; ".join(f"{a}<->{b}" for a, b in cycles[:10])
                report.errors.append(_clip_report_error(f"deadlock cycles detected: {cycle_desc}"))
        except Exception as e:  # noqa: BLE001 — 死锁检测失败不阻断调度主链
            logger.debug("deadlock check failed: %s", e)

    def _tick_peer_wait_refresh(self, report: DispatcherReport) -> None:
        """peer wait 解析接入 tick——WAITING_FOR_PEER 卡批量解析
        （回复到达解除回 READY / 超时升级 manager / 超时升级 meeting）。
        此前 refresh_waiting_tasks 生产零调用，等待卡永不流转。解除与
        meeting 升级（metadata 变更）均落库。"""
        if self.communication is None or not hasattr(self.communication, "refresh_waiting_tasks"):
            return
        try:
            peer_items = [
                item for item in self._owned_items() if item.phase == Phase.WAITING_FOR_PEER
            ]
            if not peer_items:
                return
            resolved = self.communication.refresh_waiting_tasks(peer_items)
            resolved_ids: list[str] = []
            for item in resolved:
                self.store.save(item)
                resolved_ids.append(item.id)
            # meeting 类卡：已决议（DECIDED/CLOSED）解锁 + 超时兜底（auto_resolve
            # 手动 tick 场景无人解锁，正常决议的会议卡会永久滞留
            meeting_items = [
                item
                for item in peer_items
                if (item.metadata.get("peer_wait") or {}).get("kind") == "meeting"
            ]
            if meeting_items:
                meeting_resolved = set(
                    self.communication.auto_resolve_stale_meetings(meeting_items)
                )
                for item in meeting_items:
                    if item.id in meeting_resolved:
                        self.store.save(item)
                        resolved_ids.append(item.id)
            # meeting 升级未解除但 metadata 变更（escalated_from/meeting_room_id）→ 落库；
            for item in peer_items:
                if item.id in resolved_ids:
                    continue
                wait = item.metadata.get("peer_wait") or {}
                if wait.get("kind") == "meeting" and wait.get("escalated_from"):
                    fingerprint = (item.id, str(wait.get("meeting_room_id", "")))
                    if fingerprint in self._persisted_meeting_upgrades:
                        continue
                    self.store.save(item)
                    self._persisted_meeting_upgrades.add(fingerprint)
            if resolved_ids:
                report.metadata["peer_wait_resolved"] = resolved_ids
        except Exception as e:  # noqa: BLE001 — wait 解析失败不阻断调度主链
            logger.debug("peer wait refresh failed: %s", e)

    # -- Core tick --

    async def tick(self) -> DispatcherReport:
        """功能：调度入口——进程内锁串行化（防并发 tick 重叠；跨进程由 CAS 兜底）。"""
        async with self._tick_lock:
            return await self._tick_once()

    async def _tick_once(self) -> DispatcherReport:
        report = DispatcherReport(
            tick_id=str(uuid.uuid4()),
            started_at=datetime.now(UTC),
        )
        persist_health = getattr(self.org_engine, "persist_health", None)
        if persist_health is not None:
            ph = persist_health()
            report.metadata["org_persist"] = ph
            if ph.get("degraded"):
                report.errors.append(
                    _clip_report_error(
                        "org persist degraded: memory/disk split, manual persist required "
                        f"({ph.get('last_error', '')})"
                    )
                )

        # 卡组等待图，检出互等环告警。
        self._tick_deadlock_check(report)
        self._tick_peer_wait_refresh(report)
        self._tick_stuck_focus_check(report)

        if not self.config.enable_auto_process:
            return report

        if self.group_rounds is not None:
            try:
                self.group_rounds.tick()
            except Exception:
                logger.debug("group round tick failed", exc_info=True)

        # 单次 tick 也驱动 reactivation sweeper 扫描一次——CLI/API 生产
        # 链路是单次 tick()（run_forever 常驻循环零调用方），否则
        # 卡重激活在生产链路永不触发
        # 实例缓存 + 最小间隔节流（API 高频点击时防全表扫描 × inbox 文件 IO 线性放大）
        sweeper = self._cached_sweeper
        if sweeper is None:
            sweeper = self._build_reactivation_sweeper()
            self._cached_sweeper = sweeper
        if sweeper is not None:
            now_mono = time.monotonic()
            if now_mono - self._last_sweep_at >= _SWEEP_MIN_INTERVAL:
                self._last_sweep_at = now_mono
                try:
                    await sweeper._tick()
                except Exception as e:  # noqa: BLE001 — sweeper 失败不阻断调度主链
                    logger.debug("reactivation sweep failed: %s", e)

        runtime = self.runtime
        token = runtime.use_state(runtime.create_state())
        try:
            await self._tick_work_items(report, runtime)
        finally:
            runtime.reset_state(token)

        return report

    def recover_stale_claims(self) -> int:
        """崩溃恢复——把 phase 停在 in-flight 但无活跃执行登记（进程崩溃后
        _in_flight_work_item_ids 为空）的卡转 READY 重新入队调度。

        phases (RUNNING/WAITING_FOR_PEER/WAITING_FOR_CHILDREN/PAUSED) 可被
        重新认领。被动评审态（AWAITING_*）不恢复（由评审链推进）。返回恢复数。
        启动流程（CLI/API）应在首次 tick 前调用本方法。
        """
        _recoverable = frozenset(
            {
                Phase.RUNNING,
                Phase.WAITING_FOR_PEER,
                Phase.WAITING_FOR_CHILDREN,
                Phase.PAUSED,
            }
        )
        recovered = 0
        now = datetime.now(UTC)
        for item in self._owned_items():
            if item.phase not in _recoverable:
                continue
            if item.id in self._in_flight_work_item_ids:
                continue
            # 不在本进程内存——另一实例持有效租约 = 正在执行，不得碰；
            claim = item.metadata.get("execution_claim") or {}
            lease_expires_at = claim.get("lease_expires_at")
            if lease_expires_at:
                try:
                    expires = datetime.fromisoformat(str(lease_expires_at))
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if now < expires:
                        continue
                except (TypeError, ValueError):
                    pass  # 租约损坏视为无租约，可接管
            elif not claim and item.phase != Phase.RUNNING:
                # 正常流程 WAITING_FOR_PEER/CHILDREN 与 PAUSED 本就不持租约
                continue
            metadata = dict(item.metadata or {})
            metadata.pop("execution_claim", None)
            if item.phase == Phase.WAITING_FOR_PEER:
                # 再次触发（重复升级/死锁路径），且 metadata 所有权污染
                metadata.pop("peer_wait", None)
                metadata.pop("peer_wait_released", None)
            # 崩溃中断，超过阈值转 FAILED（承认结果未知，不盲目重试
            recovery_count = int(metadata.get("recovery_count", 0) or 0) + 1
            metadata["recovery_count"] = recovery_count
            item.metadata = metadata
            if recovery_count > MAX_RECOVERY_COUNT:
                item.phase = Phase.FAILED
                # （库内 revision 已变）则 save_checked 失败，不得覆盖其状态
                if self.store.save_checked(item, getattr(item, "revision", 0)):
                    recovered += 1
                continue
            item.phase = Phase.READY
            if self.store.save_checked(item, getattr(item, "revision", 0)):
                recovered += 1
        return recovered

    def _ensure_chain_for_completed_planner(self, report: DispatcherReport) -> None:
        """功能：顶层计划卡完成但未派发子卡时，系统按白名单自动建串行链。

        模型不保证调用 spawn_thread_task（计划可能完整但 0 子卡）。
        系统侧兜底：按 `allowed_delegate_role_ids` 顺序建子卡，第一张 QUEUED，
        其余 WAITING_DEPENDENCIES（chain_step 执行轮），依赖逐环放行；父卡登记
        子卡依赖供最终聚合；plan 模式下发的执行卡带 mode_gate 等切执行模式。
        """
        items = self._owned_items()
        child_parent_ids = {str(item.parent_id or "") for item in items if item.parent_id}
        for parent in items:
            if parent.phase != Phase.APPROVED:
                continue
            metadata = dict(parent.metadata or {})
            if metadata.get("_auto_chain_built"):
                continue
            allowed = [
                str(role_id).strip()
                for role_id in (metadata.get("allowed_delegate_role_ids") or [])
                if str(role_id).strip()
            ]
            if not allowed:
                continue
            if str(parent.id) in child_parent_ids:
                _apply_and_save_checked(
                    self.store,
                    parent,
                    lambda target: target.metadata.update({"_auto_chain_built": True}),
                )
                continue
            origin_session = str(metadata.get("origin_session_id") or "")
            project_id = str(metadata.get("project_id") or "")
            thread_id = str(metadata.get("thread_id") or "")
            window_id = str(metadata.get("window_id") or "")
            # 上游计划全文（父卡产出）——子卡的任务简报必须带上，否则员工不知道
            # 本环节要交付什么
            parent_brief = str(
                (getattr(parent, "runtime_metadata", None) or {}).get("_last_output") or ""
            ).strip()[:6000]
            base_phase = Phase.QUEUED
            prev_id = ""
            child_ids: list[str] = []
            # 只给「计划里实际提到」的角色建链：纯问答/简单回复的父卡输出不含
            # 任何角色名，不能按组织拓扑预建全部下游卡（否则每次对话都堆一串
            # 「XX：本环节交付」空待办）。
            role_infos: list[tuple[str, str, str]] = []
            for role_id in allowed:
                role = (
                    self.org_engine.get_role(role_id)
                    if hasattr(self.org_engine, "get_role")
                    else None
                )
                role_name = str(getattr(role, "name", "") or role_id)
                responsibility = str(getattr(role, "responsibility", "") or "").strip()
                if role_id in parent_brief or (role_name and role_name in parent_brief):
                    role_infos.append((role_id, role_name, responsibility))
            if not role_infos:
                _apply_and_save_checked(
                    self.store,
                    parent,
                    lambda target: target.metadata.update({"_auto_chain_built": True}),
                )
                continue
            for role_id, role_name, responsibility in role_infos:
                child_metadata: dict[str, Any] = {
                    "source": "auto_chain",
                    "auto_chain_parent": str(parent.id),
                    META_RUNTIME_OWNER: str(metadata.get(META_RUNTIME_OWNER) or self.runtime_owner),
                }
                if origin_session:
                    child_metadata["origin_session_id"] = origin_session
                if project_id:
                    child_metadata["project_id"] = project_id
                if thread_id:
                    child_metadata["thread_id"] = thread_id
                if window_id:
                    child_metadata["window_id"] = window_id
                created_mode = str(metadata.get("created_mode") or "")
                if created_mode:
                    child_metadata["created_mode"] = created_mode
                    if created_mode == "plan":
                        child_metadata["mode_gate"] = "plan"
                phase = base_phase
                if prev_id and self.config.serial_chain:
                    child_metadata["dependency_work_item_ids"] = [prev_id]
                    child_metadata["chain_step"] = True
                    phase = Phase.WAITING_DEPENDENCIES
                description = (
                    f"按上游全局计划执行本环节。\n\n"
                    f"【上游计划全文】\n{parent_brief}\n\n"
                    f"【本环节角色】{role_name}：{responsibility or '完成本环节交付物'}\n"
                    f"【本环节范围】完成上游计划中指派给「{role_name}」的**全部**环节"
                    f"（可能对应多个 R 编号：深研含 R1 与 R3、数据含 R2、设计含 R4 与 R4b）；"
                    f"不要只做第一个环节，也不要代做其他角色的环节。\n"
                    f"【本环节边界】只产出本角色职责内的交付物；不要遍历系统目录；"
                    f"完成后按「任务 #N 已完成」格式汇报并给出交付文件路径。"
                )
                child = create_work_item(
                    title=_auto_chain_title(parent_brief, role_name, role_id),
                    turn_type=WorkItemTurnType.FOLLOWUP,
                    description=description,
                    assignee_role=role_id,
                    parent_id=str(parent.id),
                    phase=phase,
                    metadata=child_metadata,
                )
                self.store.save(child)
                child_ids.append(str(child.id))
                prev_id = str(child.id)
            _apply_and_save_checked(
                self.store,
                parent,
                lambda target: target.metadata.update(
                    {
                        "dependency_work_item_ids": list(child_ids),
                        "delegated_children_pending": True,
                        "frontier": "waiting_for_children",
                        "_auto_chain_built": True,
                    }
                ),
            )
            report.transitions.append((parent.id, Phase.APPROVED.value, Phase.APPROVED.value))

    def _reactivate_parents_with_done_children(self, report: DispatcherReport) -> None:
        """功能：父卡聚合补位——子卡全部终态且父卡已完成时重新激活父卡做综合轮。

        派发屏障可能因并发写丢失（执行期工具写库后旧快照覆盖），此时父卡已
        approved 而子卡仍在跑；子卡全部结束后必须把父卡拉回综合轮，否则最终交付
        无人汇总。激活一次后打 `_synthesis_done` 标记防重复。
        """
        items = self._owned_items()
        children_by_parent: dict[str, list[WorkItem]] = {}
        for item in items:
            parent_id = str(getattr(item, "parent_id", "") or "").strip()
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(item)
        for parent_id, children in children_by_parent.items():
            parent = self.store.get(parent_id)
            if parent is None or parent.phase != Phase.APPROVED:
                continue
            metadata = dict(parent.metadata or {})
            if metadata.get("_synthesis_done") or metadata.get("synthesis_turn_started"):
                continue
            if any(not is_terminal(child.phase) for child in children):
                continue
            metadata["dependency_work_item_ids"] = [str(child.id) for child in children]
            metadata["delegated_children_pending"] = False
            metadata["frontier"] = "resumed"
            metadata["_synthesis_done"] = True
            parent.metadata = metadata
            try:
                transition_work_item(parent, Phase.READY, self.store, reason=REACTIVATION_REASON)
                report.transitions.append((parent.id, Phase.APPROVED.value, Phase.READY.value))
            except Exception as exc:  # noqa: BLE001 — 单卡激活失败不阻断调度
                logger.debug("parent reactivation failed for %s: %s", parent.id, exc)

    async def _tick_work_items(self, report: DispatcherReport, runtime: CompanyRuntime) -> None:
        # 依赖 hook 的机制感知不到依赖传播产生的 READY 卡）
        refresh_transitions: list[tuple[str, str, str]] = []
        refresh_errors: list[str] = []
        refresh_dependents(
            self.store,
            transitions_out=refresh_transitions,
            errors_out=refresh_errors,
            runtime_owner=self.runtime_owner,
        )
        report.transitions.extend(refresh_transitions)
        report.errors.extend(refresh_errors)
        if self.config.serial_chain:
            self._ensure_chain_for_completed_planner(report)
        self._reactivate_parents_with_done_children(report)
        items = self._owned_items()
        if not items:
            return

        # 其他进程的 recover 见有效租约即跳过（零新增线程）
        for item in items:
            if item.id in self._in_flight_work_item_ids:
                if not self.store.renew_execution_lease(
                    item.id, self.instance_id, self._EXECUTION_LEASE_SECONDS
                ):
                    logger.warning("lease renewal lost for %s", item.id)

        rework_items: list[WorkItem] = []
        in_progress_items: list[WorkItem] = []
        in_review_items: list[WorkItem] = []
        for item in items:
            if item.phase == Phase.READY_FOR_REWORK:
                rework_items.append(item)
            elif item.phase == Phase.RUNNING:
                in_progress_items.append(item)
            elif item.phase in (
                Phase.AWAITING_MANAGER_REVIEW,
                Phase.AWAITING_HUMAN,
                Phase.NEEDS_ATTENTION,
                Phase.WAITING_FOR_PEER,
                Phase.WAITING_FOR_CHILDREN,
            ):
                in_review_items.append(item)

        # 串行落账。语义保真：认领/释放/失败口径与串行版逐条一致（失败只影响该项
        # 座位 finally 释放）；每 tick 预算与 todo 阶段同口径
        rework_limit = max(1, min(self.config.max_concurrent, self.config.max_items_per_tick))
        picked_rework: list[tuple[str, str, str, str]] = []
        for item in rework_items:
            if len(picked_rework) >= rework_limit:
                break
            if item.assignee_role and not self.claim(item.assignee_role, item.id):
                continue
            # 同口径；原并行改造只在 finally 写 released，看板漏计 rework 座位占用
            if item.assignee_role and item.assignee_role not in report.claimed:
                report.claimed.append(item.assignee_role)
            picked_rework.append(
                (item.id, item.assignee_role or "", item.assignee_role or "", item.phase.value)
            )
        if picked_rework and self.seat_executor is not None:
            # READY/READY_FOR_REWORK→RUNNING + 写租约，不在此手动 transition）
            try:
                outcomes = await self._run_picked(picked_rework)
            except BaseException:
                self._release_picked_seats(report, picked_rework)
                raise
            for work_item_id, role_id, claimed_role, old_phase, ok, err, duration_ms in outcomes:
                # F2b：rework 执行同样落 per-agent 时间线（字段与 todo 一致）
                report.timeline.append((work_item_id, role_id, duration_ms, ok))
                try:
                    if not ok:
                        logger.debug("rework item %s failed: %s", work_item_id, err)
                        report.errors.append(_clip_report_error(f"rework {work_item_id}: {err}"))
                        if claimed_role:
                            runtime.release_work_item(work_item_id, role_id=claimed_role)
                        continue
                    item = self.store.get(work_item_id)
                    if item is not None:
                        report.processed.append(work_item_id)
                        report.transitions.append((item.id, old_phase, item.phase.value))
                        if item.assignee_role:
                            runtime.release_work_item(item.id, role_id=item.assignee_role)
                finally:
                    if claimed_role:
                        self.release(claimed_role, work_item_id)
                        # 原串行版有而并行改造的 finally 丢了（released 恒空）
                        report.released.append(claimed_role)
        elif picked_rework:
            # 无执行者（纯队列模式）：按原语义释放已认领座位，不空转
            for _work_item_id, _role_id, claimed_role, _old_phase in picked_rework:
                if claimed_role:
                    self.release(claimed_role, _work_item_id)
                    report.released.append(claimed_role)

        await self._dispatch_todo_items(report, runtime, items)
        self._tick_gate_items(in_progress_items, report, runtime)

        for item in in_review_items:
            # 会抛 InvalidPhaseTransitionError 中断整轮，须跳过
            if item.phase not in (Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN):
                continue
            if not self._review_decision_ready(item):
                # 宽松自动化：员工卡提交后停在经理评审且无任何评审意见时自动写
                # approve 放行（下一 tick 走既有 approve 路径）；需强审的卡显式写
                # review_verdict 或 force_additional_review 即可跳过本支
                if item.phase == Phase.AWAITING_MANAGER_REVIEW and self.config.auto_approve_reviews:
                    base_metadata = dict(item.metadata or {})
                    base_metadata["review_verdict"] = "approve"
                    item.metadata = base_metadata
                    self.store.save_checked(item, getattr(item, "revision", 0))
                continue
            try:
                old_phase = item.phase.value
                rv = item.metadata.get("review_verdict", "")
                if rv == "approve":
                    if item.metadata.get("force_additional_review"):
                        # AWAITING_HUMAN 等人工确认；清 verdict + 标志防止
                        # 同轮/下轮重复消费死循环（人工评审后重新写 verdict）
                        base_metadata = dict(item.metadata or {})
                        base_metadata.pop("review_verdict", None)
                        base_metadata.pop("structured_review_verdict", None)
                        base_metadata.pop("force_additional_review", None)
                        base_metadata["force_additional_review_applied"] = True
                        base_metadata["human_review_reason"] = "reorg forced additional review"
                        item.metadata = base_metadata
                        item = transition_work_item(
                            item,
                            Phase.AWAITING_HUMAN,
                            self.store,
                            reason="review: forced human re-review",
                        )
                    else:
                        # 评审态时自动通过，绕过人工审核
                        # 同写入双向清除 rework 痕迹——rework_feedback 置空 +
                        base_metadata = dict(item.metadata or {})
                        base_metadata.pop("review_verdict", None)
                        base_metadata.pop("structured_review_verdict", None)
                        base_metadata["rework_feedback"] = ""
                        base_metadata["review_rework_count"] = 0
                        base_metadata.pop("user_rework_directive", None)
                        base_metadata.pop("rework_previous_output", None)
                        item.metadata = base_metadata
                        item = transition_work_item(
                            item, Phase.APPROVED, self.store, reason="review: approved"
                        )
                        self._maybe_record_project_delivery(item)
                elif rv == "rework":
                    base_metadata = dict(item.metadata or {})
                    structured = base_metadata.get("structured_review_verdict")
                    feedback = (
                        str(structured.get("reason") or structured.get("feedback") or "").strip()
                        if isinstance(structured, dict)
                        else ""
                    )
                    prior_count = int(base_metadata.get("review_rework_count", 0) or 0)
                    if prior_count >= self.config.max_review_reworks:
                        # rework 达上限强制 auto_done——
                        # 不无限循环，清空 feedback 供后续正常轮
                        base_metadata["rework_feedback"] = ""
                        base_metadata["review_rework_cap_reached_auto_done"] = True
                        base_metadata.pop("review_verdict", None)
                        base_metadata.pop("structured_review_verdict", None)
                        base_metadata.pop("user_rework_directive", None)
                        base_metadata.pop("rework_previous_output", None)
                        item.metadata = base_metadata
                        item = transition_work_item(
                            item, Phase.APPROVED, self.store, reason="review: rework cap reached"
                        )
                        self._maybe_record_project_delivery(item)
                    else:
                        base_metadata["rework_feedback"] = (
                            feedback or str(base_metadata.get("rework_feedback", "") or "").strip()
                        )
                        base_metadata["review_rework_count"] = prior_count + 1
                        # `directive or feedback` 遮蔽本轮评审反馈）
                        base_metadata.pop("user_rework_directive", None)
                        base_metadata.pop("rework_previous_output", None)
                        # rework → 执行 → gate PASS → 再进评审态 → 自动再 rework 无限循环
                        base_metadata.pop("review_verdict", None)
                        base_metadata.pop("structured_review_verdict", None)
                        item.metadata = base_metadata
                        item = transition_work_item(
                            item,
                            Phase.READY_FOR_REWORK,
                            self.store,
                            reason="review: rework requested",
                        )
                else:
                    # 未知 verdict 不再静默滞留——记录错误，保持 in_review 供人工介入
                    report.errors.append(
                        _clip_report_error(
                            f"review {item.id}: unknown review_verdict {rv!r}, item stays in review"
                        )
                    )
                report.transitions.append((item.id, old_phase, item.phase.value))
            except Exception as e:  # noqa: BLE001 — 单卡评审失败不应中断整轮调度
                logger.debug("review item %s failed: %s", item.id, e)
                report.errors.append(_clip_report_error(f"review {item.id}: {e}"))

        self._tick_settlement_fallback(report)

        if self.hook_registry:
            for item_id, old_phase, new_phase in report.transitions:
                # enqueue_session_work_on_runnable_hook 依赖 extra 才能
                hook_item = self.store.get(item_id)
                item_meta = dict(hook_item.metadata or {}) if hook_item is not None else {}
                extra: dict[str, Any] = {}
                sid = item_meta.get("session_id", "") or item_meta.get("parent_session_id", "")
                if sid:
                    extra["session_id"] = str(sid)
                if hook_item is not None and hook_item.assignee_role:
                    extra["role_key"] = hook_item.assignee_role
                ctx = HookContext(
                    work_item_id=item_id,
                    source_phase=coerce_phase(old_phase),
                    target_phase=coerce_phase(new_phase),
                    reason="dispatcher tick",
                    timestamp=datetime.now(UTC),
                    extra=extra or None,
                )
                self.hook_registry.execute("dispatcher_tick", ctx)

        # 生产零调用（运行时快照永不落库）；每 tick 轻量记录当前成员会话状态
        if self.seat_executor is not None:
            try:
                await self.seat_executor.checkpoint(runtime)
            except Exception:
                logger.debug("seat checkpoint failed", exc_info=True)

    def _tick_settlement_fallback(self, report: DispatcherReport) -> None:
        """CM-15：依赖先失败后创建的卡无 hook 事件驱动 frontier，tick 必须兜底收敛。

        dispatch 阶段 auto-progress 把 QUEUED→READY 后，本 tick 首轮 refresh
        （位于 dispatch 之前）已无法感知该卡的依赖失败——它在首轮 refresh 时
        还是 QUEUED。这里先补一轮 refresh 让依赖传播生效（READY→
        WAITING_DEPENDENCIES），再用 ``has_pending_settlement_release`` 幂等
        判断是否需要第二轮 refresh 完成 failure-triage release。上限为
        max_concurrent，防止死循环。
        """
        settlement_transitions: list[tuple[str, str, str]] = []
        settlement_errors: list[str] = []
        refresh_dependents(
            self.store,
            transitions_out=settlement_transitions,
            errors_out=settlement_errors,
            runtime_owner=self.runtime_owner,
        )
        for _ in range(max(1, self.config.max_concurrent)):
            by_id = {work_item_id(item): item for item in self._owned_items()}
            if not has_pending_settlement_release(by_id):
                break
            refresh_dependents(
                self.store,
                transitions_out=settlement_transitions,
                errors_out=settlement_errors,
                runtime_owner=self.runtime_owner,
            )
        report.errors.extend(settlement_errors)
        # 引起的 READY→WAITING_DEPENDENCIES 等转换（serial queue 唤醒丢失）
        for t in settlement_transitions:
            if t not in report.transitions:
                report.transitions.append(t)

    def _gate_to_needs_attention(
        self,
        item: WorkItem,
        gate_result: Any,
        report: DispatcherReport,
        runtime: CompanyRuntime,
    ) -> WorkItem:
        item = transition_work_item(
            item,
            Phase.NEEDS_ATTENTION,
            self.store,
            reason=f"gate: {gate_result.verdict.value} — {gate_result.reason}",
        )
        if item.assignee_role:
            runtime.release_work_item(item.id, role_id=item.assignee_role)
            self.release(item.assignee_role, item.id)
            report.released.append(item.assignee_role)
        return item

    def _tick_gate_items(
        self, gate_items: list[WorkItem], report: DispatcherReport, runtime: CompanyRuntime
    ) -> None:
        now = datetime.now(UTC)
        for item in gate_items:
            # 执行中（本进程 in-flight / 他进程有效租约）的卡尚无 _last_output——
            if item.id in self._in_flight_work_item_ids:
                continue
            claim = item.metadata.get("execution_claim") or {}
            lease_expires_at = claim.get("lease_expires_at")
            if lease_expires_at:
                try:
                    expires = datetime.fromisoformat(str(lease_expires_at))
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if now < expires:
                        continue
                except (TypeError, ValueError):
                    pass
            if item.runtime_metadata.get(META_TASK_STATUS) == "awaiting_approval":
                # （否则人读文案非空会被误 PASS 推进；空输出则进 REWORK 空转循环）。
                before_phase = item.phase.value
                item = transition_work_item(
                    item,
                    Phase.NEEDS_ATTENTION,
                    self.store,
                    reason="approval pending: awaiting human decision",
                )
                report.transitions.append((item.id, before_phase, item.phase.value))
                if item.assignee_role:
                    # 对称释放 runtime claim 与座位（与 _gate_to_needs_attention 同口径）
                    runtime.release_work_item(item.id, role_id=item.assignee_role)
                    self.release(item.assignee_role, item.id)
                    report.released.append(item.assignee_role)
                continue
            if (
                bool(item.metadata.get("group_pass"))
                and item.runtime_metadata.get(META_TASK_STATUS) != "failed"
            ):
                if item.phase != Phase.APPROVED:
                    before_phase = item.phase.value
                    try:
                        item = transition_work_item(
                            item,
                            Phase.APPROVED,
                            self.store,
                            reason="group pass",
                        )
                        report.transitions.append((item.id, before_phase, item.phase.value))
                    except InvalidPhaseTransitionError:
                        logger.debug("group pass gate skipped for %s from %s", item.id, item.phase)
                if item.assignee_role:
                    runtime.release_work_item(item.id, role_id=item.assignee_role)
                    self.release(item.assignee_role, item.id)
                    report.released.append(item.assignee_role)
                continue

            gate_result = self.gate_harness.evaluate(
                item.id,
                item.description or item.title,
                item.runtime_metadata.get(META_LAST_OUTPUT, ""),
                task_status=item.runtime_metadata.get(META_TASK_STATUS, "completed"),
                self_reported_risks=item.runtime_metadata.get(META_RISKS, []),
            )
            if gate_result.verdict == GateVerdict.PASS:
                guard_missing = _artifact_guard_missing(item)
                if guard_missing:
                    gate_result = GateResult(
                        GateVerdict.REWORK,
                        "ArtifactGuard: 声称完成但以下产物不可读回：" + ", ".join(guard_missing),
                        evidence={**gate_result.evidence, "missing_artifacts": guard_missing},
                    )
            report.gate_results.append((item.id, gate_result.verdict.value))

            old_phase = item.phase.value
            if gate_result.verdict == GateVerdict.PASS:
                # review_rework_count/review_verdict 已完成使命，卡转评审态后
                # turn_mode 不得再判 REWORK、_review_decision_ready 不得误判已评审
                if (
                    str(item.metadata.get("rework_feedback", "") or "").strip()
                    or int(item.metadata.get("review_rework_count", 0) or 0)
                    or item.metadata.get("structured_review_verdict")
                    or item.metadata.get("review_verdict")
                ):
                    meta = dict(item.metadata or {})
                    meta["rework_feedback"] = ""
                    meta["review_rework_count"] = 0
                    # 该卡后续所有执行轮的返工段）
                    meta.pop("user_rework_directive", None)
                    meta.pop("rework_previous_output", None)
                    # 未来 `in` 检查会误判「已评审」
                    meta.pop("structured_review_verdict", None)
                    meta.pop("review_verdict", None)
                    item.metadata = meta
                guard_keys = (
                    "manager_dispatch_guard_attempts",
                    "manager_dispatch_guard_violation",
                    "manager_dispatch_guard_unresolved",
                )
                if any(key in item.metadata for key in guard_keys):
                    meta = dict(item.metadata or {})
                    for key in guard_keys:
                        meta.pop(key, None)
                    item.metadata = meta
                # 聊天回合卡（chat_*）产出即终态——对话不是任务，不进评审滞留
                is_chat_turn = str(item.metadata.get("source") or "") in (
                    "chat_mention",
                    "chat_dm",
                )
                # 评审人 = 父卡负责人；顶层任务没有父卡即没有审批员
                reviewer_role = ""
                if not is_chat_turn and item.parent_id:
                    parent_item = self.store.get(item.parent_id)
                    if parent_item is not None:
                        reviewer_role = str(parent_item.assignee_role or "").strip()
                # 没有审批员或自动通过：产出即终态，不停在待审
                needs_review = bool(reviewer_role) and not self.config.auto_approve_reviews
                item = transition_work_item(
                    item,
                    Phase.AWAITING_MANAGER_REVIEW if needs_review else Phase.APPROVED,
                    self.store,
                    reason="gate: pass (review)" if needs_review else "gate: pass",
                )
                if item.assignee_role:
                    runtime.release_work_item(item.id, role_id=item.assignee_role)
                    self.release(item.assignee_role, item.id)
                    report.released.append(item.assignee_role)
                if needs_review:
                    review_card = build_review_card(item, reviewer_role, self.store)
                    if review_card is not None:
                        self.signal_dispatcher_wake()
                        logger.info(
                            "review card %s created for %s (reviewer=%s)",
                            review_card.id,
                            item.id,
                            reviewer_role,
                        )
            elif gate_result.verdict in (
                GateVerdict.REWORK,
                GateVerdict.ESCALATE,
                GateVerdict.REPLAN,
                GateVerdict.BLOCKED,
            ):
                item = self._gate_to_needs_attention(item, gate_result, report, runtime)
            report.transitions.append((item.id, old_phase, item.phase.value))

    def _ensure_runnable_roles_staffed(
        self, items: list[WorkItem], report: DispatcherReport
    ) -> None:
        """功能：运行前补员——注册角色缺座位时自动补员工+座位；不可补时标记可见失败。"""
        from bandi.company.staffing import ensure_role_staffed

        recruiter = getattr(self, "recruiter", None)
        org_engine = getattr(self, "org_engine", None)
        if recruiter is None or not hasattr(recruiter, "get_seat") or org_engine is None:
            return
        runnable_phases = (Phase.QUEUED, Phase.READY, Phase.READY_FOR_REWORK)
        for item in items:
            role_id = str(item.assignee_role or "").strip()
            if not role_id or item.phase not in runnable_phases:
                continue
            if self.recruiter.get_seat(role_id) is not None:
                continue
            outcome = ensure_role_staffed(self.org_engine, self.recruiter, role_id)
            if outcome in ("ok", "skip"):
                if outcome == "ok":
                    logger.info("auto-staffed role %s for work item %s", role_id, item.id)
                continue
            if item.metadata.get("staffing_blocked_reason"):
                continue
            reason = (
                f"role '{role_id}' is not registered"
                if outcome == "role-not-registered"
                else f"role '{role_id}' staffing failed"
            )

            def _mark(target: WorkItem, text: str = reason) -> None:
                metadata = dict(target.metadata or {})
                metadata["staffing_blocked_reason"] = text
                target.metadata = metadata

            _apply_and_save_checked(self.store, item, _mark)
            report.errors.append(_clip_report_error(f"staffing blocked: {item.id} {reason}"))

    def _release_picked_seats(self, report: Any, picked: list[Any]) -> None:
        """（12）：释放 picked 中已认领座位（幂等）——_run_picked 异常/取消
        时座位不得永久 BUSY（后续 dispatch 恒 False、角色卡死）。"""
        for _work_item_id, _role_id, claimed_role, _old_phase in picked:
            if claimed_role:
                self.release(claimed_role, _work_item_id)
                report.released.append(claimed_role)

    async def _dispatch_todo_items(
        self,
        report: DispatcherReport,
        runtime: CompanyRuntime,
        items: list[WorkItem],
    ) -> None:
        """role-queue 驱动的 todo 调度：入队 → 按成员会话消费 → claim → run → 释放。

        无 assignee 的 QUEUED 项先 auto-progress 到 READY（不执行），保持既有契约。
        """
        for item in items:
            if item.phase == Phase.QUEUED and not (item.assignee_role or "").strip():
                old_phase = item.phase.value
                item = transition_work_item(
                    item, Phase.READY, self.store, reason="dispatcher: auto-progress"
                )
                report.transitions.append((item.id, old_phase, item.phase.value))

        self._ensure_runnable_roles_staffed(items, report)
        runtime.enqueue_runnable_work_items(items)
        for agent in self.org_engine.list_agents():
            runtime.ensure_role_member_session(agent.role_id)

        sessions = runtime.sorted_member_sessions()
        if self.seat_executor is None:
            # 无执行者：只做纯队列推进（QUEUED→READY），**不执行、不记 processed**。
            # 这条路径逐字保留原串行实现——并行改造只针对「真的有执行」的场景
            # 不改变无执行者的既有契约（tick 只推进相位）。
            self._dispatch_phases_only(report, runtime, sessions)
            return

        # 每 tick 总量上限 = 单 tick 硬顶；并发上限由 max_concurrent 在循环内控制
        total_limit = max(1, min(self.config.max_items_per_tick, len(items) or 1))
        concurrency = max(1, min(self.config.max_concurrent, total_limit))

        # 卡级接续：任一卡完成即释放槽位并补位下一张（不再等整批 gather 结束）。
        # 认领仍是串行的——只在「有空槽」且该角色当前无在跑卡时挑一张。
        picked_all: list[tuple[str, str, str, str]] = []
        running: dict[asyncio.Task[Any], tuple[str, str, str, str]] = {}
        busy_roles: set[str] = set()
        skipped: set[str] = set()
        dispatched = 0
        deadline = (
            time.monotonic() + self.config.tick_budget_sec
            if self.config.tick_budget_sec and self.config.tick_budget_sec > 0
            else 0.0
        )
        try:
            while True:
                while len(running) < concurrency and dispatched < total_limit:
                    if deadline and time.monotonic() >= deadline:
                        break
                    candidate = self._pick_dispatch_candidate(
                        report, runtime, sessions, busy_roles, skipped
                    )
                    if candidate is None:
                        break
                    busy_roles.add(candidate[1])
                    picked_all.append(candidate)
                    dispatched += 1
                    running[asyncio.create_task(self._run_one(candidate))] = candidate
                if not running:
                    break
                done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    entry = running.pop(task)
                    busy_roles.discard(entry[1])
                    self._account_dispatch_outcome(
                        report, runtime, self._outcome_from_task(task, entry)
                    )
        except BaseException:
            self._release_picked_seats(report, picked_all)
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise

    def _pick_dispatch_candidate(
        self,
        report: DispatcherReport,
        runtime: CompanyRuntime,
        sessions: list[Any],
        busy_roles: set[str],
        skipped: set[str],
    ) -> tuple[str, str, str, str] | None:
        """串行挑选并认领一张可执行卡；不可派发项回队（本轮不再重试）。"""
        for session in sessions:
            role_id = getattr(session, "role_id", "")
            if role_id in busy_roles:
                continue
            if getattr(session, "status", "") == "running":
                continue
            work_item_id = runtime.pop_next_work_item_id(role_id)
            if work_item_id is None:
                continue
            item = self.store.get(work_item_id)
            if work_item_id in skipped:
                self._requeue_unrunnable(runtime, item)
                continue
            not_dispatchable = item is None or not runtime.is_dispatchable(item)
            plan_gate = (
                item is not None
                and str((item.metadata or {}).get("mode_gate", "") or "").strip().lower() == "plan"
            )
            if not_dispatchable or plan_gate:
                skipped.add(work_item_id)
                self._requeue_unrunnable(runtime, item)
                logger.debug("todo item %s not dispatchable this tick; requeued", work_item_id)
                continue
            if work_item_id in runtime.state.claimed_work_item_ids:
                skipped.add(work_item_id)
                self._requeue_unrunnable(runtime, item)
                continue
            if item.assignee_role and not self.claim(item.assignee_role, item.id):
                skipped.add(work_item_id)
                self._requeue_unrunnable(runtime, item)
                continue
            claimed_role = item.assignee_role or ""
            try:
                old_phase = item.phase.value
                runtime.claim_work_item(work_item_id, role_id=role_id)
                return (work_item_id, role_id, claimed_role, old_phase)
            except Exception as e:  # noqa: BLE001 — 工作项状态变更不应阻断调度循环
                logger.debug("todo item %s failed: %s", work_item_id, e)
                report.errors.append(_clip_report_error(f"todo {work_item_id}: {e}"))
                skipped.add(work_item_id)
                self._requeue_unrunnable(runtime, item)
                if claimed_role:
                    self.release(claimed_role, work_item_id)
                    report.released.append(claimed_role)
        return None

    @staticmethod
    def _requeue_unrunnable(runtime: CompanyRuntime, item: Any) -> None:
        # 功能：把弹出但未派发的工作项放回队列（供后续 tick 重试，不丢调度机会）
        requeue = getattr(runtime, "requeue_work_item", None)
        if item is None or requeue is None:
            return
        try:
            requeue(item)
        except Exception:
            logger.debug("todo item %s requeue failed", getattr(item, "id", ""), exc_info=True)

    def _account_dispatch_outcome(
        self,
        report: DispatcherReport,
        runtime: CompanyRuntime,
        outcome: tuple[str, str, str, str, bool, str, float],
    ) -> None:
        # 功能：单卡落账（processed/errors/transitions/座位释放）——完成即落，不等整批
        work_item_id, role_id, claimed_role, old_phase, ok, err, duration_ms = outcome
        item = self.store.get(work_item_id)
        report.timeline.append((work_item_id, role_id, duration_ms, ok))
        if ok:
            report.processed.append(work_item_id)
        else:
            report.errors.append(_clip_report_error(f"todo {work_item_id}: {err}"))
        runtime.release_work_item(work_item_id, role_id=role_id)
        if role_id not in report.claimed:
            report.claimed.append(role_id)
        report.transitions.append(
            (work_item_id, old_phase, item.phase.value if item is not None else old_phase)
        )
        if claimed_role:
            self.release(claimed_role, work_item_id)
            report.released.append(claimed_role)

    def _dispatch_phases_only(
        self, report: DispatcherReport, runtime: CompanyRuntime, sessions: list[Any]
    ) -> None:
        """无执行者路径（原实现保留）：只推进相位，不执行、不记 processed。"""
        limit = max(1, min(self.config.max_concurrent, self.config.max_items_per_tick))
        picked_count = 0
        for session in sessions:
            if picked_count >= limit:
                break
            if session.status == "running":
                continue
            work_item_id = runtime.pop_next_work_item_id(session.role_id)
            if work_item_id is None:
                continue
            item = self.store.get(work_item_id)
            if item is None or not runtime.is_dispatchable(item):
                continue
            # plan 模式门：未切执行模式的派发卡先挂起（不耗 LLM、不弹审批）
            if str((item.metadata or {}).get("mode_gate", "") or "").strip().lower() == "plan":
                continue
            if work_item_id in runtime.state.claimed_work_item_ids:
                continue
            if item.assignee_role and not self.claim(item.assignee_role, item.id):
                continue
            claimed_role = item.assignee_role or ""
            try:
                old_phase = item.phase.value
                if item.phase == Phase.QUEUED:
                    # 无执行者：纯队列推进 QUEUED→READY（不执行），让依赖传播/
                    # 后续调度感知。不覆盖他进程认领。
                    item = transition_work_item(
                        item, Phase.READY, self.store, reason="dispatcher: auto-progress"
                    )
                runtime.claim_work_item(work_item_id, role_id=session.role_id)
                picked_count += 1
                if session.role_id not in report.claimed:
                    report.claimed.append(session.role_id)
                report.transitions.append(
                    (
                        work_item_id,
                        old_phase,
                        item.phase.value if item is not None else old_phase,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("todo item %s failed: %s", work_item_id, e)
                report.errors.append(_clip_report_error(f"todo {work_item_id}: {e}"))
            finally:
                if claimed_role:
                    self.release(claimed_role, work_item_id)
                    report.released.append(claimed_role)

    async def _run_one(
        self, entry: tuple[str, str, str, str]
    ) -> tuple[str, str, str, str, bool, str, float]:
        """执行单卡（每项独立超时；单点失败不传染其余项）。"""
        work_item_id, role_id, claimed_role, old_phase = entry
        timeout = self.config.per_item_timeout_sec
        started = time.monotonic()
        ok = True
        err = ""
        try:
            if timeout and timeout > 0:
                seat_result = await asyncio.wait_for(
                    self.run_work_item(work_item_id), timeout=timeout
                )
            else:
                seat_result = await self.run_work_item(work_item_id)
            if not getattr(seat_result, "success", True):
                ok = False
                err = str(getattr(seat_result, "output", "") or "execution failed")
        except TimeoutError:
            logger.debug("todo item %s timed out after %ss", work_item_id, timeout)
            ok, err = False, f"timeout after {timeout}s"
            # 超时降级文本：整体取消会让 gate 读空 _last_output 判
            # 「Empty output」→ needs_attention 空卡；以人话收口让用户
            # 看到状态而非空白。
            try:
                timeout_item = self.store.get(work_item_id)
                if timeout_item is not None:
                    timeout_item = _apply_and_save_checked(
                        self.store,
                        timeout_item,
                        lambda target: target.runtime_metadata.update(
                            {
                                META_LAST_OUTPUT: (
                                    "（本轮因等待授权或运行超时中断，未产出完整结果。"
                                    "若为权限卡等待：批复后重试本卡即可接续推进。）"
                                ),
                                META_TASK_STATUS: "timeout_fallback",
                            }
                        ),
                    )
                    if timeout_item.phase == Phase.RUNNING:
                        transition_work_item(
                            timeout_item,
                            Phase.NEEDS_ATTENTION,
                            self.store,
                            reason=f"execution timeout after {timeout}s",
                        )
            except Exception:
                logger.debug("timeout fallback write failed", exc_info=True)
        except Exception as e:  # noqa: BLE001 — 单点失败只影响该项
            from bandi.utils.errors import error_text

            logger.debug("todo item %s failed: %s", work_item_id, error_text(e))
            ok, err = False, error_text(e)
        duration_ms = (time.monotonic() - started) * 1000.0
        return (work_item_id, role_id, claimed_role, old_phase, ok, err, duration_ms)

    @staticmethod
    def _outcome_from_task(
        task: asyncio.Task[Any], entry: tuple[str, str, str, str]
    ) -> tuple[str, str, str, str, bool, str, float]:
        # 功能：从已完成任务取结果；任务层异常收敛为该项失败（取消照常上抛）
        try:
            result = task.result()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — 任务层兜底
            return (*entry, False, str(exc), 0.0)
        if isinstance(result, BaseException):
            return (*entry, False, str(result), 0.0)
        return result

    async def _run_picked(
        self, picked: list[tuple[str, str, str, str]]
    ) -> list[tuple[str, str, str, str, bool, str, float]]:
        """并行执行已认领的工作项，返回 `(item_id, role_id, claimed_role, old_phase, ok, err)`。

        语义逐条对齐参考源：
          * 并发闸门 `Semaphore(max_concurrent)` —— LeAgent workflow/engine/executor.py:326；
          * `gather(return_exceptions=True)` 且每项内部再兜一层——**单点失败绝不取消
            其余项**（LeAgent rules/engine.py:522、dsh workflow runtime.ts:400）；
          * **每项独立 `wait_for`** —— LeAgent cron/executor.py:186。刻意避开其
            executor.py:338 的批次级超时：那会在任一项超时时 `cancel()` 整批。
        """
        sem = asyncio.Semaphore(max(1, self.config.max_concurrent))

        async def _limited(entry: tuple[str, str, str, str]) -> Any:
            async with sem:
                return await self._run_one(entry)

        gathered = await asyncio.gather(
            *(_limited(entry) for entry in picked), return_exceptions=True
        )
        outcomes: list[tuple[str, str, str, str, bool, str, float]] = []
        for result, entry in zip(gathered, picked, strict=True):
            if isinstance(result, BaseException):
                # gather 层兜底（_run_one 已吞异常；此处防未来改动破坏该保证）
                outcomes.append((*entry, False, str(result), 0.0))
            else:
                outcomes.append(result)
        return outcomes

    @staticmethod
    def _review_decision_ready(item: WorkItem) -> bool:
        return "review_verdict" in item.metadata

    def _resolve_execution_agent(self, item: WorkItem) -> WorkItem:
        """CM-16：执行前解析最终执行 agent（native/external），结果写
        runtime_metadata 供 seat executor / external broker 消费。

        item.metadata 中的 ``selected_execution_agent`` /
        ``preferred_external_agent`` / ``force_native_execution``，解析后写入
        ``effective_execution_agent`` / ``assigned_external_agent`` /
        ``force_native_execution``。
        """
        from bandi.company.recruiter import resolve_effective_execution_agent

        metadata = dict(item.metadata or {})
        effective, assigned_external, force_native = resolve_effective_execution_agent(
            metadata.get("selected_execution_agent"),
            metadata.get("preferred_external_agent"),
            force_native_execution=_as_bool(metadata.get("force_native_execution", False)),
        )

        def _apply_resolution(target: WorkItem) -> None:
            target.runtime_metadata["effective_execution_agent"] = effective
            if assigned_external:
                target.runtime_metadata["assigned_external_agent"] = assigned_external
            target.runtime_metadata["force_native_execution"] = force_native

        return _apply_and_save_checked(self.store, item, _apply_resolution)

    def _apply_dispatch_guard(
        self,
        *,
        item: WorkItem,
        output: str,
        pre_dependency_ids: set[str],
        pre_child_ids: set[str],
    ) -> tuple[WorkItem, bool]:
        """功能：DELEGATE 轮无 board 变更时——限次退回重试，超限标记 unresolved 放行。"""
        metadata_now = dict(item.metadata or {})
        # 纯问答/计划未提及任何可委派角色：本轮没有派发义务，不重跑（否则同一轮
        # 重复调用 LLM，token 被放大数倍）。
        allowed = [
            str(role_id).strip()
            for role_id in (metadata_now.get("allowed_delegate_role_ids") or [])
            if str(role_id).strip()
        ]
        if allowed:
            mentioned = False
            for role_id in allowed:
                role = (
                    self.org_engine.get_role(role_id)
                    if hasattr(self.org_engine, "get_role")
                    else None
                )
                role_name = str(getattr(role, "name", "") or role_id)
                if role_id in output or (role_name and role_name in output):
                    mentioned = True
                    break
            if not mentioned:
                return item, False
        now_dependency_ids = {str(v) for v in (metadata_now.get("dependency_work_item_ids") or [])}
        now_child_ids = {
            w.id for w in self._owned_items() if str(getattr(w, "parent_id", "") or "") == item.id
        }
        board_changed = (
            bool(now_dependency_ids - pre_dependency_ids)
            or bool(now_child_ids - pre_child_ids)
            or bool(metadata_now.get("manager_board_mutation_performed"))
        )
        attempts = int(metadata_now.get("manager_dispatch_guard_attempts") or 0)
        verdict = evaluate_dispatch_guard(
            is_delegate_turn=True,
            board_changed=board_changed,
            has_justification="NO_DELEGATION_JUSTIFICATION:" in output,
            attempts=attempts,
        )
        if verdict == DispatchGuardVerdict.OK:
            return item, False
        if verdict == DispatchGuardVerdict.RETRY:

            def _apply_retry(target: WorkItem) -> None:
                metadata = dict(target.metadata or {})
                metadata["manager_dispatch_guard_attempts"] = attempts + 1
                metadata["manager_dispatch_guard_violation"] = _DISPATCH_GUARD_VIOLATION
                target.metadata = metadata

            item = _apply_and_save_checked(self.store, item, _apply_retry)
            if item.phase == Phase.RUNNING:
                item = transition_work_item(
                    item,
                    Phase.READY,
                    self.store,
                    reason="dispatch guard: requeue for delegation retry",
                )
            logger.info("dispatch guard requeue %s (attempt %d)", item.id, attempts + 1)
            return item, True

        def _apply_unresolved(target: WorkItem) -> None:
            metadata = dict(target.metadata or {})
            metadata["manager_dispatch_guard_unresolved"] = True
            metadata.pop("manager_dispatch_guard_violation", None)
            target.metadata = metadata

        item = _apply_and_save_checked(self.store, item, _apply_unresolved)
        logger.warning("dispatch guard unresolved: %s (no delegation after retries)", item.id)
        return item, False

    def _apply_review_bridge(self, item: WorkItem, result: SeatResult) -> tuple[WorkItem, bool]:
        """功能：评审卡回合结束——解析输出裁决并机械应用到目标卡；解析失败重试一次后升级人工。"""
        target_id = str((item.metadata or {}).get("review_target_work_item_id") or "").strip()
        verdict = parse_review_verdict(result.output or "")
        if verdict is None:
            attempts = int((item.metadata or {}).get("review_parse_attempts") or 0)
            if attempts < 1:

                def _apply_parse_retry(target: WorkItem) -> None:
                    metadata = dict(target.metadata or {})
                    metadata["review_parse_attempts"] = attempts + 1
                    metadata["review_parse_failure_hint"] = True
                    target.metadata = metadata

                item = _apply_and_save_checked(self.store, item, _apply_parse_retry)
                if item.phase == Phase.RUNNING:
                    item = transition_work_item(
                        item,
                        Phase.READY,
                        self.store,
                        reason="review bridge: verdict unparsable, retry",
                    )
                logger.info("review bridge requeue %s (parse attempt %d)", item.id, attempts + 1)
                return item, True
            self._escalate_review_target(target_id, "review verdict unparsable after retry")
            return (
                self._finish_review_card(item, reason="review bridge: unparsable escalated"),
                False,
            )
        feedback = verdict.summary
        if verdict.blocking_issues:
            feedback = (
                feedback + "\n" + "\n".join(f"- {issue}" for issue in verdict.blocking_issues)
            ).strip()
        try:
            self.review_work_item(
                target_id,
                "approve" if verdict.verdict == "approve" else "rework",
                feedback=feedback,
                reviewer=item.assignee_role or "",
            )
        except Exception as exc:  # noqa: BLE001 — 目标卡状态竞态不阻断评审卡收尾
            logger.warning("review bridge apply failed for %s: %s", target_id, exc)
            self._escalate_review_target(target_id, f"review verdict apply failed: {exc}")
        return self._finish_review_card(item, reason=f"review bridge: {verdict.verdict}"), False

    def _escalate_review_target(self, target_id: str, reason: str) -> None:
        # 功能：评审自动链失败出口——目标卡升级人工评审（fail-closed 不静默滞留）。
        if not target_id:
            return
        target = self.store.get(target_id)
        if target is None:
            return
        if target.phase in (Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN):
            transition_work_item(target, Phase.AWAITING_HUMAN, self.store, reason=reason)

    def _finish_review_card(self, item: WorkItem, *, reason: str) -> WorkItem:
        # 功能：评审卡收尾（RUNNING→APPROVED；裁决已应用，评审卡自身可收口）。
        fresh = self.store.get(item.id) or item
        if fresh.phase == Phase.RUNNING:
            fresh = transition_work_item(fresh, Phase.APPROVED, self.store, reason=reason)
        elif fresh.phase == Phase.NEEDS_ATTENTION:
            # 评审卡自身空输出/超时被判 needs_attention，但 verdict 已应用到目标卡：
            # 评审使命完成，直接收口为 approved（防评审卡悬挂待处理噪音）
            try:
                fresh = transition_work_item(fresh, Phase.APPROVED, self.store, reason=reason)
            except Exception:  # noqa: BLE001 — 非法迁移兜底：直接落 approved
                fresh.phase = Phase.APPROVED
                fresh.updated_at = datetime.now(UTC)
                self.store.save(fresh)
        return fresh

    # -- Work item execution --

    def _mark_failed_execution(self, work_item_id: str, result: SeatResult) -> None:
        """功能：执行失败终态（ESCALATED/FAILED 等）释放 running——转 needs_attention。"""
        reason = str((result.metadata or {}).get("terminal_reason") or "")
        if result.success or reason == "AWAITING_APPROVAL":
            return
        fresh = self.store.get(work_item_id)
        if fresh is None or fresh.phase != Phase.RUNNING:
            return
        transition_work_item(
            fresh,
            Phase.NEEDS_ATTENTION,
            self.store,
            reason=f"execution failed: {reason or 'unknown'}",
        )

    async def run_work_item(self, work_item_id: str) -> SeatResult:
        item = self.store.get(work_item_id)
        if item is None:
            return SeatResult(success=False, output=f"WorkItem {work_item_id} not found")

        if self.seat_executor is None:
            return SeatResult(success=False, output="No seat executor configured")

        # assignee_role 是角色 id；解析真实员工 id（metadata 指派优先，否则角色反查）
        # 再交给 seat 执行器，否则 per-employee llm_config 查不到（换大脑断链）。
        runtime = self._build_runtime(
            session_scope_id=item.id,
            role_id=item.assignee_role or "unknown",
            employee_id=self._resolve_assignee_employee_id(item),
            team_instance_id=self._home_team_for_role(item.assignee_role),
            # 按真实项目维度隔离；缺失才回退 communication 默认值（"company"）
            project_id=str((item.metadata or {}).get("project_id") or ""),
            # 员工上下文按对话窗口隔离（群聊会话/员工单聊各一窗口）；
            # 旧卡无 window_id 时回退既有跨任务历史空间（兼容存量）。
            history_scope_id=str((item.metadata or {}).get("window_id") or "")
            or _COMPANY_HISTORY_SCOPE,
        )
        # QUEUED 卡一步原子认领（claim_from_queued：QUEUED→RUNNING+写租约
        # 不再用无条件 transition_work_item 预转——旧快照 save 覆盖会抹掉他进程
        if not self._try_claim_work_item(item.id):
            return SeatResult(
                success=False,
                output=f"WorkItem {work_item_id} already claimed or in-flight",
            )
        # 否则入口旧 item 的整体回写（_resolve_execution_agent save）会抹掉租约
        item = self.store.get(work_item_id) or item
        if item.assignee_role:
            runtime.claim_work_item(item.id, role_id=item.assignee_role)
        abort_controller = AbortController()
        self._work_item_aborts[work_item_id] = abort_controller
        # 每 _LEASE_RENEW_INTERVAL 刷新租约，防他进程误判 stale 接管。
        renew_task = asyncio.create_task(self._lease_renewal_loop(work_item_id, abort_controller))
        # 防 _resolve_execution_agent 等前段异常时 finally 引用未绑定变量
        executor: SeatExecutor | None = self.seat_executor
        try:
            item = self._resolve_execution_agent(item)
            # （路线 ）：按解析结果选执行器——选中外部且非 force_native
            # 走 ExternalSeatExecutor；缺位显式失败（不静默回退 native）。
            executor, assigned_external = self._select_seat_executor(item)
            if executor is None:
                # 释放租约 + RUNNING→READY，然后显式失败。
                self.store.release_execution_claim(item.id, instance_id=self.instance_id)
                item = self.store.get(work_item_id) or item
                if item is not None and item.phase == Phase.RUNNING:
                    item = transition_work_item(
                        item, Phase.READY, self.store, reason="external executor not configured"
                    )
                return SeatResult(
                    success=False,
                    output=(
                        f"external execution agent '{assigned_external}' assigned "
                        "but no ExternalSeatExecutor configured"
                    ),
                )
            if self.task_preparer is not None:
                prep = await self.task_preparer.prepare(item, self.store, self.recruiter, runtime)
                if not prep.ready:
                    self.store.release_execution_claim(item.id, instance_id=self.instance_id)
                    item = self.store.get(work_item_id) or item
                    if item is not None and item.phase == Phase.RUNNING:
                        item = transition_work_item(
                            item, Phase.READY, self.store, reason="task preparation failed"
                        )
                    return SeatResult(
                        success=False, output=f"Task preparation failed: {prep.reason}"
                    )
            await executor.prepare_seat(runtime, item)
            self._active_executions[work_item_id] = (executor, runtime)
            # 仅刷新 updated_at 与 last_transition_reason），保留以对齐执行语义。
            item = transition_work_item(
                item, Phase.RUNNING, self.store, reason="dispatcher: execute"
            )
            pre_turn_mode = infer_turn_mode(item)
            pre_dependency_ids: set[str] = set()
            pre_child_ids: set[str] = set()
            if pre_turn_mode == TurnMode.DELEGATE:
                pre_dependency_ids = {
                    str(v) for v in (item.metadata or {}).get("dependency_work_item_ids") or []
                }
                pre_child_ids = {
                    w.id
                    for w in self._owned_items()
                    if str(getattr(w, "parent_id", "") or "") == item.id
                }
            exec_task = asyncio.create_task(executor.run_turn(runtime, item))
            self._execution_tasks[work_item_id] = exec_task
            try:
                result = await exec_task
            except asyncio.CancelledError:
                # 仅用户中断走收尾；进程关停等外部取消继续向上传播
                if not abort_controller.should_abort:
                    raise
                return self._mark_interrupted(
                    work_item_id, abort_controller.reason or "user_interrupt"
                )
            finally:
                self._execution_tasks.pop(work_item_id, None)

            if abort_controller.should_abort:
                return self._mark_interrupted(
                    work_item_id, abort_controller.reason or "user_interrupt"
                )

            item = self.store.get(work_item_id)
            if item is not None:

                def _apply_result(target: WorkItem) -> None:
                    # 后台标记一次性：执行结束即清除，避免返工/重激活继续全自动放行。
                    target.metadata.pop("background_run", None)
                    if bool((target.metadata or {}).get("group_chat")):
                        target.metadata["group_pass"] = is_pass_output(result.output or "")
                    if _is_deliverable_output(result):
                        # 违规时保留原始产物（_last_output 不写错误文案）
                        target.runtime_metadata[META_LAST_OUTPUT] = (result.metadata or {}).get(
                            "_original_output", result.output
                        )
                    reason_now = str((result.metadata or {}).get("terminal_reason") or "")
                    if reason_now == "AWAITING_APPROVAL":
                        member_sid = runtime.member_session_id()
                        target.runtime_metadata["_approval_session_id"] = member_sid
                        call_id, tool_name = _approval_ref_for_result(result, member_sid)
                        if call_id:
                            target.runtime_metadata["_approval_tool_call_id"] = call_id
                        if tool_name:
                            target.runtime_metadata["_approval_tool_name"] = tool_name
                    else:
                        target.runtime_metadata.pop("_approval_session_id", None)
                        target.runtime_metadata.pop("_approval_tool_call_id", None)
                        target.runtime_metadata.pop("_approval_tool_name", None)
                    target.runtime_metadata[META_TASK_STATUS] = _seat_task_status(result)
                    logger.info(
                        "work item %s applied: reason=%s deliverable=%s success=%s output_len=%d",
                        target.id,
                        reason_now or "(none)",
                        _is_deliverable_output(result),
                        result.success,
                        len(result.output or ""),
                    )
                    # 重新 store.get 后被丢弃——桥接生产链断点修复）
                    sop_progress = (result.metadata or {}).get(SOP_PROGRESS_KEY)
                    if sop_progress:
                        target.runtime_metadata[SOP_PROGRESS_KEY] = sop_progress

                item = _apply_and_save_checked(self.store, item, _apply_result)
                group_pass = bool((item.metadata or {}).get("group_chat")) and is_pass_output(
                    result.output or ""
                )
                children = [
                    child
                    for child in self._owned_items()
                    if str(getattr(child, "parent_id", "") or "") == item.id
                ]
                children_pending = bool(children) and any(
                    not is_terminal(child.phase) for child in children
                )
                if (
                    pre_turn_mode == TurnMode.DELEGATE
                    and result.success
                    and not abort_controller.should_abort
                    and str((result.metadata or {}).get("terminal_reason") or "")
                    != "AWAITING_APPROVAL"
                ):
                    item, guard_retry = self._apply_dispatch_guard(
                        item=item,
                        output=result.output or "",
                        pre_dependency_ids=pre_dependency_ids,
                        pre_child_ids=pre_child_ids,
                    )
                    if guard_retry:
                        return SeatResult(
                            success=True,
                            output="dispatch guard: turn requeued for delegation retry",
                            metadata={"dispatch_guard_retry": True},
                        )
                if (
                    bool((item.metadata or {}).get("review_execution_work_item"))
                    and result.success
                    and not abort_controller.should_abort
                ):
                    item, review_retry = self._apply_review_bridge(item, result)
                    if review_retry:
                        return SeatResult(
                            success=True,
                            output="review bridge: verdict unparsable, requeued",
                            metadata={"review_parse_retry": True},
                        )
                # 卡转 NEEDS_ATTENTION——不静默放行，标记人工介入而非走 gate 正常流。
                violation = (result.metadata or {}).get("output_contract_violation")
                if violation is not None:
                    item = self.store.get(work_item_id) or item
                    if item is not None and item.phase == Phase.RUNNING:
                        item = transition_work_item(
                            item,
                            Phase.NEEDS_ATTENTION,
                            self.store,
                            reason=(
                                "output_contract_violation: "
                                f"{json.dumps(violation, ensure_ascii=False)}"
                            ),
                        )
                # employee_assignment 元数据由 dispatcher 写入（record_work_item_completion
                # 依赖它定位员工），缺员工归属时静默跳过。
                if children_pending and item.phase == Phase.RUNNING:
                    child_ids = [str(child.id) for child in children]

                    def _mark_held(target: WorkItem) -> None:
                        existing = [
                            str(item)
                            for item in (target.metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY) or [])
                        ]
                        target.metadata[DEPENDENCY_WORK_ITEM_IDS_KEY] = list(
                            dict.fromkeys([*existing, *child_ids])
                        )
                        target.metadata[DELEGATED_CHILDREN_PENDING_KEY] = True
                        target.metadata[FRONTIER_KEY] = WAITING_FOR_CHILDREN_FRONTIER

                    item = _apply_and_save_checked(self.store, item, _mark_held)
                    item = transition_work_item(
                        item,
                        Phase.WAITING_FOR_CHILDREN,
                        self.store,
                        reason="waiting for delegated children",
                    )
                if not group_pass and not children_pending:
                    self._record_work_item_completion(item, result)
                # 按卡 metadata.project_id 路由项目线程（项目群/单聊卡回写对应项目
                # 否则 ThreadView 收不到产出）；add_message 自身失败不阻断执行。
                thread_id = (item.metadata or {}).get("thread_id")
                manager = resolve_thread_manager_for(
                    item,
                    default=self.threads,
                    resolver=getattr(self, "thread_manager_resolver", None),
                )
                if (
                    thread_id
                    and manager is not None
                    and not group_pass
                    and not children_pending
                    and _is_deliverable_output(result)
                ):
                    try:
                        manager.add_message(
                            thread_id,
                            sender=item.assignee_role or "unknown",
                            content=result.output or "",
                            message_id=item.id,
                            # 过程快照随回复持久化——刷新后过程块可回看
                            process_events=list((result.metadata or {}).get("process_events") or [])
                            or None,
                        )
                    except Exception:
                        logger.debug("thread message write failed", exc_info=True)
                # （与上面线程回写 message_id 同源）；origin_session_id 的卡
                # （群聊广播/CLI 派单）不受影响；失败不阻断执行链
                session_writer = getattr(self, "session_writer", None)
                origin_session = str((item.metadata or {}).get("origin_session_id") or "")
                if (
                    session_writer is not None
                    and origin_session
                    and not group_pass
                    and not children_pending
                    and _is_deliverable_output(result)
                ):
                    try:
                        session_writer(
                            origin_session,
                            item.assignee_role or "",
                            self._role_display_name(item.assignee_role or ""),
                            result.output or "",
                            item.id,
                            process_events=list((result.metadata or {}).get("process_events") or [])
                            or None,
                        )
                    except Exception:
                        logger.debug("session history write-back failed", exc_info=True)
                if self.group_rounds is not None:
                    try:
                        self.group_rounds.on_slot_finished(item, result)
                        maybe_handoff = getattr(self.group_rounds, "maybe_handoff", None)
                        if maybe_handoff is not None:
                            await maybe_handoff(item, result)
                    except Exception:
                        logger.debug("group round advance failed", exc_info=True)
                self._mark_failed_execution(work_item_id, result)

            return result
        except Exception as e:  # noqa: BLE001 — 执行错误通过 SeatResult 返回，不抛到调度器
            from bandi.utils.errors import error_text

            logger.debug("work item execution failed: %s", error_text(e))
            return SeatResult(success=False, output=f"Execution error: {error_text(e)}")
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            with self._in_flight_lock:
                self._in_flight_work_item_ids.discard(work_item_id)
            # 永久跳过该卡（卡死在 RUNNING 无人推进）；释放幂等（无 claim no-op）
            self.store.release_execution_claim(work_item_id, instance_id=self.instance_id)
            # 执行结束 → 收尾该员工会话仍挂起的交互（防孤儿卡在重放中反复出现、
            # 用户重答无效果；挂起方已随执行退出，答复无接收方）
            cleanup = getattr(self, "employee_interaction_cleanup", None)
            if cleanup is not None:
                try:
                    cleanup(runtime.member_session_id())
                except Exception:
                    logger.debug("employee interaction cleanup failed", exc_info=True)
            self._work_item_aborts.pop(work_item_id, None)
            self._active_executions.pop(work_item_id, None)
            if executor is not None:
                try:
                    await executor.interrupt(runtime)
                except Exception:
                    logger.debug("executor interrupt in finally failed", exc_info=True)

    def _select_seat_executor(self, item: WorkItem) -> tuple[SeatExecutor | None, str]:
        """按 runtime_metadata 解析结果选择执行器。

        返回 (executor, error)。assigned_external_agent 非空且未 force_native
        时走 ExternalSeatExecutor（路线 ）；其缺位 → (None, assigned) 显式
        失败语义（不静默回退 native——根因「解析有、执行无」的反面）。
        """
        runtime_meta = item.runtime_metadata or {}
        assigned = str(runtime_meta.get("assigned_external_agent") or "").strip()
        force_native = _as_bool(runtime_meta.get("force_native_execution", False))
        if not assigned or force_native:
            return self.seat_executor, ""
        if self.external_seat_executor is None:
            return None, assigned
        return self.external_seat_executor, ""

    def _try_claim_work_item(self, item_id: str) -> bool:
        """原子认领：QUEUED 卡走 claim_from_queued（一步转 RUNNING）
        READY/READY_FOR_REWORK 走 claim_for_execution——两者都是数据库级 CAS
        他实例有效租约/非目标 phase 直接拒，不覆盖。成功才登记 in-flight。"""
        if self.store.claim_from_queued(
            item_id, self.instance_id, self._EXECUTION_LEASE_SECONDS
        ) or self.store.claim_for_execution(
            item_id, self.instance_id, self._EXECUTION_LEASE_SECONDS
        ):
            with self._in_flight_lock:
                self._in_flight_work_item_ids.add(item_id)
            return True
        return False

    _EXECUTION_LEASE_SECONDS = 120
    _LEASE_RENEW_INTERVAL = 60

    async def _lease_renewal_loop(
        self, item_id: str, abort_controller: AbortController | None = None
    ) -> None:
        """长任务执行期间周期续约：每 _LEASE_RENEW_INTERVAL 秒刷新本实例
        租约；他进程已接管（续约失败）即中止本地执行，避免陈旧执行者继续产生副作用。"""
        while True:
            await asyncio.sleep(self._LEASE_RENEW_INTERVAL)
            if not self.store.renew_execution_lease(
                item_id, self.instance_id, self._EXECUTION_LEASE_SECONDS
            ):
                logger.warning("lease renewal lost for %s (reclaimed by another instance)", item_id)
                if abort_controller is not None:
                    abort_controller.abort("lease_lost")
                task = self._execution_tasks.get(item_id)
                if task is not None:
                    task.cancel()
                return

    def _maybe_record_project_delivery(self, item: WorkItem) -> None:
        """功能：交付卡（turn_type=DELIVER）进入终态即触发项目反思与技能晋升。

        边界：审批型交付卡停在 AWAITING_MANAGER_REVIEW 时不算交付完成（终态才触发）。
        """
        if self.employee_evolution is None or item.turn_type is not WorkItemTurnType.DELIVER:
            return
        try:
            from bandi.company.project_delivery import maybe_record_delivery_reflection

            result = maybe_record_delivery_reflection(
                store=self.store, evolution=self.employee_evolution, item=item
            )
            # A5/A6 桥接：反思后同步各员工经验分/技能到组织 metadata（前端可见）
            for reflection in (result or {}).get("reflections") or []:
                reflected_employee = str(reflection.get("employee_id") or "").strip()
                if not reflected_employee:
                    continue
                try:
                    self.employee_evolution.sync_org_employee_metadata(
                        self.org_engine,
                        reflected_employee,
                        project_id=str(reflection.get("project_id") or "").strip() or None,
                    )
                except Exception:
                    logger.debug("delivery metadata sync failed", exc_info=True)
        except Exception:  # 反思记录失败不阻断调度主链路
            logger.debug("project delivery reflection failed", exc_info=True)

    def _record_work_item_completion(self, item: WorkItem, result: SeatResult) -> None:
        """员工演进记录：写入 employee_assignment 元数据并记录完成经验。"""
        self._maybe_record_project_delivery(item)
        if self.employee_evolution is None:
            return
        role_id = item.assignee_role or ""
        if not role_id:
            return
        employee_id = self._resolve_assignee_employee_id(item)
        # 解析不到真实员工即不记录——用 role_id 冒充 employee_id 会把经验归属挂到角色上
        if not employee_id:
            return
        existing = dict(item.metadata.get("employee_assignment") or {})
        employee = self.org_engine.get_employee(employee_id)

        def _apply_assignment(target: WorkItem) -> None:
            target.metadata["employee_assignment"] = {
                "employee_id": employee_id,
                "role_id": role_id,
                "name": str(existing.get("name") or "")
                or (employee.name if employee else employee_id),
                "project_id": self.communication.project_id if self.communication else "",
            }

        item = _apply_and_save_checked(self.store, item, _apply_assignment)
        try:
            from bandi.company.employee_evolution import extract_quality_dimensions

            dimensions = extract_quality_dimensions(dict(item.metadata or {}))
            self.employee_evolution.record_work_item_completion(
                task=item,
                result_content=result.output or "",
                outcome="success" if result.success else "failed",
                feedback_summary=str(dimensions.get("feedback_summary") or ""),
                strengths=list(dimensions.get("strengths") or []),
                weaknesses=list(dimensions.get("weaknesses") or []),
                rationale=str(dimensions.get("rationale") or ""),
            )
        except Exception:  # 演进记录失败不阻断调度主链路
            logger.debug("employee evolution record failed", exc_info=True)
        # A5/A6 桥接：经验分 + 学到的技能写回组织员工 metadata（招募/前端档案同源）
        try:
            project_id = str((item.metadata or {}).get("project_id") or "").strip() or (
                self.communication.project_id if self.communication else ""
            )
            self.employee_evolution.sync_org_employee_metadata(
                self.org_engine, employee_id, project_id=project_id or None
            )
        except Exception:  # 桥接失败不阻断调度主链路
            logger.debug("employee metadata sync failed", exc_info=True)

    # -- Query --

    def get_report(self) -> KanbanSnapshot:
        """返回当前 kanban 列分组的查询快照（非 tick 报告）。"""
        items = self.store.list_all()
        grouped: dict[str, int] = {}
        for item in items:
            col = kanban_column(item.phase)
            grouped[col] = grouped.get(col, 0) + 1
        return KanbanSnapshot(captured_at=datetime.now(UTC), column_counts=grouped)
