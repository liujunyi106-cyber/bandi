# 功能：工作项实体——创建/转换/快照

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from bandi.company.phase import Phase, validate_transition


class WorkItemTurnType(Enum):
    INTAKE = "intake"
    DISPATCH = "dispatch"
    PLAN = "plan"
    SETUP = "setup"
    EXECUTE = "execute"
    REVIEW = "review"
    REPORT = "report"
    FOLLOWUP = "followup"
    MONITOR = "monitor"
    AGGREGATE = "aggregate"
    DELIVER = "deliver"
    SELF_EVOLUTION = "self_evolution"


CANONICAL_WORK_ITEM_TURN_TYPES: frozenset[str] = frozenset(t.value for t in WorkItemTurnType)

_TURN_TYPE_ALIASES: dict[str, WorkItemTurnType] = {
    "exec": WorkItemTurnType.EXECUTE,
    "run": WorkItemTurnType.EXECUTE,
    "planning": WorkItemTurnType.PLAN,
    "setup_environment": WorkItemTurnType.SETUP,
    "merge": WorkItemTurnType.AGGREGATE,
    "delivery": WorkItemTurnType.DELIVER,
    "self_evolve": WorkItemTurnType.SELF_EVOLUTION,
}


def normalize_turn_type(value: str) -> WorkItemTurnType:
    try:
        return WorkItemTurnType(value)
    except ValueError:
        pass
    alias = _TURN_TYPE_ALIASES.get(value)
    if alias is not None:
        return alias
    raise ValueError(f"Unknown turn type: {value}")


@dataclass
class WorkItem:
    id: str
    title: str
    description: str
    phase: Phase
    turn_type: WorkItemTurnType
    assignee_role: str | None
    parent_id: str | None
    children_ids: list[str]
    links: dict[str, str]
    metadata: dict[str, Any]
    runtime_metadata: dict[str, Any]
    comments: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # save_checked(expected_revision) 做冲突检测，并发修改时整轮放弃可重试。
    revision: int = 0

    def snapshot(self) -> WorkItemSnapshot:
        return WorkItemSnapshot(
            id=self.id,
            title=self.title,
            phase=self.phase,
            turn_type=self.turn_type,
            assignee_role=self.assignee_role,
        )


@dataclass
class WorkItemSnapshot:
    id: str
    title: str
    phase: Phase
    turn_type: WorkItemTurnType
    assignee_role: str | None


class WorkItemStore(Protocol):
    def save(self, item: WorkItem) -> None: ...
    def get(self, item_id: str) -> WorkItem | None: ...
    def list_by_phase(self, phase: Phase) -> list[WorkItem]: ...
    def delete(self, item_id: str) -> None: ...


def create_work_item(
    title: str,
    turn_type: WorkItemTurnType,
    description: str = "",
    assignee_role: str | None = None,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    links: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    phase: Phase = Phase.QUEUED,
) -> WorkItem:
    now = datetime.now(UTC)
    return WorkItem(
        id=str(uuid.uuid4()),
        title=title,
        description=description,
        phase=phase,
        turn_type=turn_type,
        assignee_role=assignee_role,
        parent_id=parent_id,
        children_ids=children_ids or [],
        links=links or {},
        metadata=metadata or {},
        runtime_metadata={},
        created_at=now,
        updated_at=now,
    )


REACTIVATION_REASON = "reactivated_by_inbound_comms"


def transition_work_item(
    item: WorkItem,
    target_phase: Phase,
    store: WorkItemStore,
    reason: str = "",
) -> WorkItem:
    # 并发写保护：执行期间工具可能已更新库内卡（派发屏障/子卡登记），
    # 过期快照直接 save 会覆盖这些字段——以库内最新 metadata 为准，
    # 只把本进程的 runtime 增量（_last_output 等）合并进去。
    fresh = store.get(item.id) if hasattr(store, "get") else None
    # 库内 revision 非 int（空返回/测试替身）按「无更新」处理，不触发合并
    fresh_revision = getattr(fresh, "revision", None)
    if isinstance(fresh_revision, int) and fresh_revision > int(getattr(item, "revision", 0) or 0):
        item.metadata = dict(fresh.metadata or {})
        item.runtime_metadata = {
            **dict(fresh.runtime_metadata or {}),
            **dict(item.runtime_metadata or {}),
        }
        item.revision = int(getattr(fresh, "revision", 0) or 0)
        item.phase = fresh.phase
    if item.phase == target_phase:
        if reason:
            item.runtime_metadata["last_transition_reason"] = reason
            item.updated_at = datetime.now(UTC)
            store.save(item)
            recorder = getattr(store, "record_journal", None)
            if callable(recorder):
                recorder(
                    item.id,
                    "phase_reason",
                    detail={"phase": target_phase.value, "reason": reason},
                )
        return item
    old_phase = item.phase.value
    validate_transition(item.phase, target_phase)
    if (
        item.phase == Phase.APPROVED
        and target_phase == Phase.READY
        and reason != REACTIVATION_REASON
    ):
        raise ValueError(
            f"APPROVED->READY only allowed via inbound-comms reactivation, got reason={reason!r}"
        )
    item.phase = target_phase
    item.updated_at = datetime.now(UTC)
    if reason:
        item.runtime_metadata["last_transition_reason"] = reason
    store.save(item)
    recorder = getattr(store, "record_journal", None)
    if callable(recorder):
        recorder(
            item.id,
            "phase_changed",
            detail={
                "from": old_phase,
                "to": target_phase.value,
                "reason": reason,
            },
        )
    return item


def is_hidden_work_item(item: WorkItem) -> bool:
    """功能：运行时内部卡（隐藏评审/报告卡）判定——不上用户看板。"""
    metadata = dict(item.metadata or {})
    return bool(
        metadata.get("review_execution_work_item") or metadata.get("report_execution_work_item")
    )


# 功能：纯对话轮次类型——只讨论不产出，任务看板默认不投影（可显式打开）
CHAT_ONLY_KINDS = frozenset({"query", "adjust"})
CONVERSATION_SOURCES = frozenset({"chat_mention", "chat_dm"})


def is_chat_only_work_item(item: WorkItem) -> bool:
    """功能：会话对话卡判定（chat_kind=query/adjust 或来源为群聊/单聊）——任务看板隐藏。

    `source=chat_mention/chat_dm` 是用户对话产生的承载卡，不是团队执行任务；
    真正的团队任务来自 agent_to_agent/auto_chain。与右栏计划树过滤口径一致。
    """
    metadata = dict(item.metadata or {})
    kind = str(metadata.get("chat_kind", "") or "").strip().lower()
    source = str(metadata.get("source", "") or "").strip().lower()
    return kind in CHAT_ONLY_KINDS or source in CONVERSATION_SOURCES


# 功能：历史「模板预建草稿」来源标记
_LEGACY_DRAFT_SOURCES = frozenset({"team_template"})


def is_legacy_draft_work_item(item: WorkItem) -> bool:
    """功能：历史模板草稿残留判定——PAUSED 且 source=team_template。

    应用模板只建组织、不产生任何工作项，这类卡没有任何入口能推进，
    留在看板就是永久草稿，故一律不投影。
    """
    metadata = dict(item.metadata or {})
    return (
        item.phase == Phase.PAUSED
        and str(metadata.get("source", "") or "") in _LEGACY_DRAFT_SOURCES
    )
