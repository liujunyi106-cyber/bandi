# 功能：阶段状态机——14 态 + 转换校验 + 终止判定

from __future__ import annotations

from enum import Enum

from bandi.core.exceptions import BandiError


class InvalidPhaseTransitionError(BandiError):
    def __init__(self, source: Phase, target: Phase) -> None:
        self.source = source
        self.target = target
        super().__init__(f"Invalid transition: {source.value} -> {target.value}")


class InvalidPhaseValueError(BandiError):
    def __init__(self, value: object) -> None:
        super().__init__(f"Unknown phase value `{value}`")


class Phase(Enum):
    QUEUED = "queued"
    READY = "ready"
    WAITING_DEPENDENCIES = "waiting_dependencies"
    READY_FOR_REWORK = "ready_for_rework"
    RUNNING = "running"
    WAITING_FOR_PEER = "waiting_for_peer"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    AWAITING_MANAGER_REVIEW = "awaiting_manager_review"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnMode(Enum):
    EXECUTE = "execute"
    DELEGATE = "delegate"
    REVIEW = "review"
    INTEGRATE = "integrate"
    REWORK = "rework"
    REPORT = "report"


_TERMINAL_PHASES = frozenset({Phase.APPROVED, Phase.FAILED, Phase.CANCELLED})

ALLOWED_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.QUEUED: frozenset(
        {Phase.READY, Phase.WAITING_DEPENDENCIES, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.WAITING_DEPENDENCIES: frozenset(
        {Phase.READY, Phase.READY_FOR_REWORK, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.READY: frozenset(
        {Phase.RUNNING, Phase.WAITING_DEPENDENCIES, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.READY_FOR_REWORK: frozenset(
        {Phase.RUNNING, Phase.WAITING_DEPENDENCIES, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.RUNNING: frozenset(
        {
            Phase.READY,  # 崩溃恢复出口——stale claim 释放后回队列重新调度
            Phase.WAITING_FOR_PEER,
            Phase.WAITING_FOR_CHILDREN,
            Phase.PAUSED,
            Phase.NEEDS_ATTENTION,
            Phase.AWAITING_MANAGER_REVIEW,
            Phase.AWAITING_HUMAN,
            Phase.APPROVED,
            Phase.FAILED,
            Phase.CANCELLED,
        }
    ),
    Phase.WAITING_FOR_PEER: frozenset(
        {
            Phase.READY,
            Phase.RUNNING,
            Phase.AWAITING_HUMAN,  # 会议 human 决策升级出口（communication.resolve_task_meeting_wait）
            Phase.FAILED,
            Phase.CANCELLED,
        }
    ),
    Phase.WAITING_FOR_CHILDREN: frozenset(
        {Phase.READY, Phase.RUNNING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.PAUSED: frozenset({Phase.READY, Phase.RUNNING, Phase.FAILED, Phase.CANCELLED}),
    Phase.NEEDS_ATTENTION: frozenset({Phase.RUNNING, Phase.FAILED, Phase.CANCELLED}),
    Phase.AWAITING_MANAGER_REVIEW: frozenset(
        {
            Phase.APPROVED,
            Phase.READY_FOR_REWORK,
            Phase.AWAITING_HUMAN,
            Phase.FAILED,
            Phase.CANCELLED,
        }
    ),
    Phase.AWAITING_HUMAN: frozenset(
        {Phase.APPROVED, Phase.READY_FOR_REWORK, Phase.FAILED, Phase.CANCELLED}
    ),
    # DONE->PENDING）。仅由 reactivation_sweeper 的守卫路径调用，不自动发生
    # 不参与依赖传播。is_terminal 仍为 True（kanban/verdict 语义不变）。
    Phase.APPROVED: frozenset({Phase.READY}),
    Phase.FAILED: frozenset(),
    Phase.CANCELLED: frozenset(),
}


def validate_transition(source: Phase, target: Phase) -> None:
    if target not in ALLOWED_TRANSITIONS.get(source, frozenset()):
        raise InvalidPhaseTransitionError(source, target)


def kanban_column(phase: Phase) -> str:
    if phase in (
        Phase.QUEUED,
        Phase.READY,
        Phase.WAITING_DEPENDENCIES,
        Phase.READY_FOR_REWORK,
        # 草稿（模板生成未启动）没有在跑，归待办而不是进行中
        Phase.PAUSED,
    ):
        return "todo"
    if phase in (
        Phase.RUNNING,
        Phase.WAITING_FOR_PEER,
        Phase.WAITING_FOR_CHILDREN,
        Phase.NEEDS_ATTENTION,
    ):
        return "in_progress"
    if phase in (Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN):
        return "in_review"
    return "done"


def is_terminal(phase: Phase) -> bool:
    return phase in _TERMINAL_PHASES


def is_runnable(phase: Phase) -> bool:
    return phase == Phase.RUNNING


def is_waiting_external(phase: Phase) -> bool:
    return phase in (
        Phase.WAITING_FOR_PEER,
        Phase.WAITING_FOR_CHILDREN,
        Phase.AWAITING_MANAGER_REVIEW,
        Phase.AWAITING_HUMAN,
        Phase.NEEDS_ATTENTION,
    )


def verdict(phase: Phase) -> str | None:
    mapping = {
        Phase.APPROVED: "approve",
        Phase.READY_FOR_REWORK: "rework",
        Phase.FAILED: "fail",
        Phase.CANCELLED: "cancel",
    }
    return mapping.get(phase)


def classify_turn_for_phase(phase: Phase) -> TurnMode:
    mapping: dict[Phase, TurnMode] = {
        Phase.RUNNING: TurnMode.EXECUTE,
        Phase.READY: TurnMode.DELEGATE,
        Phase.AWAITING_MANAGER_REVIEW: TurnMode.REVIEW,
        Phase.AWAITING_HUMAN: TurnMode.REVIEW,
        Phase.WAITING_FOR_CHILDREN: TurnMode.INTEGRATE,
        Phase.READY_FOR_REWORK: TurnMode.REWORK,
    }
    return mapping.get(phase, TurnMode.REPORT)


def coerce_phase(value: str | Phase) -> Phase:
    if isinstance(value, Phase):
        return value
    try:
        return Phase(value)
    except ValueError as exc:
        raise InvalidPhaseValueError(value) from exc
