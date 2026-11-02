# 功能：组织重构——变更集/迁移计划/快照/提案/事件类型 + 纯函数（静态声明，不含 ReorgManager 编排逻辑）

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from bandi.company.phase import Phase

ACTIVE_PHASES = frozenset(
    {
        Phase.QUEUED,
        Phase.READY,
        Phase.WAITING_DEPENDENCIES,
        Phase.READY_FOR_REWORK,
        Phase.WAITING_FOR_PEER,
        Phase.WAITING_FOR_CHILDREN,
        Phase.PAUSED,
        Phase.NEEDS_ATTENTION,
        Phase.AWAITING_MANAGER_REVIEW,
        Phase.AWAITING_HUMAN,
    }
)


class ReorgScope(StrEnum):
    TASK_ADJUSTMENT = "task_adjustment"
    ORG_MUTATION = "org_mutation"


class ReorgRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReorgProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DENIED = "denied"
    APPLIED = "applied"
    FAILED = "failed"
    CANCELLED = "cancelled"


_REORG_PROPOSAL_TRANSITIONS: dict[ReorgProposalStatus, frozenset[ReorgProposalStatus]] = {
    ReorgProposalStatus.PROPOSED: frozenset(
        {
            ReorgProposalStatus.APPROVED,
            ReorgProposalStatus.DENIED,
            ReorgProposalStatus.CANCELLED,
        }
    ),
    ReorgProposalStatus.APPROVED: frozenset(
        {ReorgProposalStatus.APPLIED, ReorgProposalStatus.FAILED, ReorgProposalStatus.CANCELLED}
    ),
    ReorgProposalStatus.DENIED: frozenset(),
    ReorgProposalStatus.APPLIED: frozenset(),
    ReorgProposalStatus.FAILED: frozenset(),
    ReorgProposalStatus.CANCELLED: frozenset(),
}


def validate_reorg_proposal_transition(previous: str, target: str) -> None:
    """校验提案状态转换：终态无出边；同态幂等写放行；违规抛 ValueError。"""
    if previous == target:
        return
    allowed = _REORG_PROPOSAL_TRANSITIONS.get(ReorgProposalStatus(previous), frozenset())
    if ReorgProposalStatus(target) not in allowed:
        raise ValueError(
            f"Invalid reorg proposal transition: {previous} -> {target}"
            " (terminal states are immutable)"
        )


class ReorgEventKind(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DENIED = "denied"
    APPLIED = "applied"
    MIGRATED = "migrated"
    AUTO_TASK_ADJUSTED = "auto_task_adjusted"
    FAILED = "failed"


@dataclass
class ReorgRoleChange:
    """单一角色变更指令（add/remove/replace/update）。"""

    action: str = "update"
    role_id: str = ""
    replacement_role_id: str | None = None
    role: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "role_id": self.role_id,
            "replacement_role_id": self.replacement_role_id,
            "role": dict(self.role),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgRoleChange:
        payload = dict(data or {})
        return cls(
            action=str(payload.get("action", "update") or "update"),
            role_id=str(payload.get("role_id", "") or ""),
            replacement_role_id=payload.get("replacement_role_id"),
            role=dict(payload.get("role", {}) or {}),
            reason=str(payload.get("reason", "") or ""),
        )


@dataclass
class ReorgTaskAdjustment:
    """单一任务调整指令（不改变角色拓扑）。"""

    task_id: str = ""
    action: str = "reassign"
    new_role_id: str | None = None
    priority: int | None = None
    description_append: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "new_role_id": self.new_role_id,
            "priority": self.priority,
            "description_append": self.description_append,
            "acceptance_criteria": list(self.acceptance_criteria),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgTaskAdjustment:
        payload = dict(data or {})
        return cls(
            task_id=str(payload.get("task_id", "") or ""),
            action=str(payload.get("action", "reassign") or "reassign"),
            new_role_id=payload.get("new_role_id"),
            priority=payload.get("priority"),
            description_append=str(payload.get("description_append", "") or ""),
            acceptance_criteria=list(payload.get("acceptance_criteria", []) or []),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class ReorgChangeSet:
    """一次组织变更的完整指令集（角色变更 + 任务调整）。"""

    role_changes: list[ReorgRoleChange] = field(default_factory=list)
    task_adjustments: list[ReorgTaskAdjustment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_changes": [change.to_dict() for change in self.role_changes],
            "task_adjustments": [adj.to_dict() for adj in self.task_adjustments],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgChangeSet:
        payload = dict(data or {})
        return cls(
            role_changes=[
                item if isinstance(item, ReorgRoleChange) else ReorgRoleChange.from_dict(item)
                for item in list(payload.get("role_changes", []) or [])
            ],
            task_adjustments=[
                item
                if isinstance(item, ReorgTaskAdjustment)
                else ReorgTaskAdjustment.from_dict(item)
                for item in list(payload.get("task_adjustments", []) or [])
            ],
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class ReorgMigrationPlan:
    """"""

    affected_task_ids: list[str] = field(default_factory=list)
    affected_checkpoint_ids: list[str] = field(default_factory=list)
    affected_handoff_ids: list[str] = field(default_factory=list)
    affected_employee_ids: list[str] = field(default_factory=list)
    role_mapping: dict[str, str] = field(default_factory=dict)
    invalidated_waits: list[str] = field(default_factory=list)
    migration_notes: list[str] = field(default_factory=list)
    compatibility_warnings: list[str] = field(default_factory=list)
    rollback_snapshot_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_task_ids": list(self.affected_task_ids),
            "affected_checkpoint_ids": list(self.affected_checkpoint_ids),
            "affected_handoff_ids": list(self.affected_handoff_ids),
            "affected_employee_ids": list(self.affected_employee_ids),
            "role_mapping": dict(self.role_mapping),
            "invalidated_waits": list(self.invalidated_waits),
            "migration_notes": list(self.migration_notes),
            "compatibility_warnings": list(self.compatibility_warnings),
            "rollback_snapshot_id": self.rollback_snapshot_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgMigrationPlan:
        payload = dict(data or {})
        return cls(
            affected_task_ids=list(payload.get("affected_task_ids", []) or []),
            affected_checkpoint_ids=list(payload.get("affected_checkpoint_ids", []) or []),
            affected_handoff_ids=list(payload.get("affected_handoff_ids", []) or []),
            affected_employee_ids=list(payload.get("affected_employee_ids", []) or []),
            role_mapping=dict(payload.get("role_mapping", {}) or {}),
            invalidated_waits=list(payload.get("invalidated_waits", []) or []),
            migration_notes=list(payload.get("migration_notes", []) or []),
            compatibility_warnings=list(payload.get("compatibility_warnings", []) or []),
            rollback_snapshot_id=payload.get("rollback_snapshot_id"),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class OrgSnapshot:
    """组织拓扑在某个 org_version 上的只读快照。"""

    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    project_id: str = "default"
    org_version: int = 1
    runtime_topology_version: int = 1
    company_name: str = ""
    topology: str = ""
    roles: list[dict[str, Any]] = field(default_factory=list)
    company_profile: str = "corporate"
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "project_id": self.project_id,
            "org_version": self.org_version,
            "runtime_topology_version": self.runtime_topology_version,
            "company_name": self.company_name,
            "topology": self.topology,
            "roles": [dict(role) for role in self.roles],
            "company_profile": self.company_profile,
            "active_tasks": [dict(task) for task in self.active_tasks],
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OrgSnapshot:
        payload = dict(data or {})
        created = payload.get("created_at")
        return cls(
            snapshot_id=str(payload.get("snapshot_id", "") or uuid.uuid4().hex),
            project_id=str(payload.get("project_id", "") or "default"),
            org_version=int(payload.get("org_version", 1) or 1),
            runtime_topology_version=int(payload.get("runtime_topology_version", 1) or 1),
            company_name=str(payload.get("company_name", "") or ""),
            topology=str(payload.get("topology", "") or ""),
            roles=[dict(role) for role in list(payload.get("roles", []) or [])],
            company_profile=str(payload.get("company_profile", "") or "corporate"),
            active_tasks=[dict(task) for task in list(payload.get("active_tasks", []) or [])],
            metadata=dict(payload.get("metadata", {}) or {}),
            created_at=datetime.fromisoformat(created) if created else datetime.now(UTC),
        )


@dataclass
class ReorgProposal:
    """一次待审批/待应用的组织变更提案。"""

    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    project_id: str = "default"
    session_id: str | None = None
    task_id: str | None = None
    initiated_by: str = "owner"
    source_role_id: str = ""
    scope: ReorgScope = ReorgScope.ORG_MUTATION
    risk_level: ReorgRiskLevel = ReorgRiskLevel.MEDIUM
    status: ReorgProposalStatus = ReorgProposalStatus.PROPOSED
    title: str = ""
    summary: str = ""
    rationale: str = ""
    user_confirmation_required: bool = True
    old_org_version: int = 1
    new_org_version: int = 1
    old_runtime_topology_version: int = 1
    new_runtime_topology_version: int = 1
    changeset: ReorgChangeSet = field(default_factory=ReorgChangeSet)
    migration_plan: ReorgMigrationPlan = field(default_factory=ReorgMigrationPlan)
    impact_summary: dict[str, Any] = field(default_factory=dict)
    approval_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "initiated_by": self.initiated_by,
            "source_role_id": self.source_role_id,
            "scope": self.scope.value,
            "risk_level": self.risk_level.value,
            "status": self.status.value,
            "title": self.title,
            "summary": self.summary,
            "rationale": self.rationale,
            "user_confirmation_required": self.user_confirmation_required,
            "old_org_version": self.old_org_version,
            "new_org_version": self.new_org_version,
            "old_runtime_topology_version": self.old_runtime_topology_version,
            "new_runtime_topology_version": self.new_runtime_topology_version,
            "changeset": self.changeset.to_dict(),
            "migration_plan": self.migration_plan.to_dict(),
            "impact_summary": dict(self.impact_summary),
            "approval_notes": self.approval_notes,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgProposal:
        payload = dict(data or {})
        created = payload.get("created_at")
        updated = payload.get("updated_at")
        # 脏数据（拼写错/旧版本枚举）抛 ValueError 崩溃整个反序列化链
        scope_raw = payload.get("scope", "org_mutation")
        try:
            scope = ReorgScope(scope_raw)
        except ValueError:
            scope = ReorgScope("org_mutation")
        risk_raw = payload.get("risk_level", "medium")
        try:
            risk_level = ReorgRiskLevel(risk_raw)
        except ValueError:
            risk_level = ReorgRiskLevel("medium")
        status_raw = payload.get("status", "proposed")
        try:
            status = ReorgProposalStatus(status_raw)
        except ValueError:
            status = ReorgProposalStatus("proposed")
        return cls(
            proposal_id=str(payload.get("proposal_id", "") or uuid.uuid4().hex),
            project_id=str(payload.get("project_id", "") or "default"),
            session_id=payload.get("session_id"),
            task_id=payload.get("task_id"),
            initiated_by=str(payload.get("initiated_by", "") or "owner"),
            source_role_id=str(payload.get("source_role_id", "") or ""),
            scope=scope,
            risk_level=risk_level,
            status=status,
            title=str(payload.get("title", "") or ""),
            summary=str(payload.get("summary", "") or ""),
            rationale=str(payload.get("rationale", "") or ""),
            user_confirmation_required=bool(payload.get("user_confirmation_required", True)),
            old_org_version=int(payload.get("old_org_version", 1) or 1),
            new_org_version=int(payload.get("new_org_version", 1) or 1),
            old_runtime_topology_version=int(payload.get("old_runtime_topology_version", 1) or 1),
            new_runtime_topology_version=int(payload.get("new_runtime_topology_version", 1) or 1),
            changeset=ReorgChangeSet.from_dict(payload.get("changeset")),
            migration_plan=ReorgMigrationPlan.from_dict(payload.get("migration_plan")),
            impact_summary=dict(payload.get("impact_summary", {}) or {}),
            approval_notes=str(payload.get("approval_notes", "") or ""),
            metadata=dict(payload.get("metadata", {}) or {}),
            created_at=datetime.fromisoformat(created) if created else datetime.now(UTC),
            updated_at=datetime.fromisoformat(updated) if updated else datetime.now(UTC),
        )


@dataclass
class ReorgEventRecord:
    """一次组织变更生命周期事件（proposed/approved/applied/...）。"""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    proposal_id: str = ""
    project_id: str = "default"
    event_kind: ReorgEventKind = ReorgEventKind.PROPOSED
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "event_kind": self.event_kind.value,
            "summary": self.summary,
            "details": dict(self.details),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReorgEventRecord:
        payload = dict(data or {})
        created = payload.get("created_at")
        # ReorgEventKind(...) 直接构造，脏数据（拼写错/旧版本枚举）抛
        # ValueError 崩溃反序列化链
        kind_raw = payload.get("event_kind", "proposed")
        try:
            event_kind = ReorgEventKind(kind_raw)
        except ValueError:
            event_kind = ReorgEventKind.PROPOSED
        return cls(
            event_id=str(payload.get("event_id", "") or uuid.uuid4().hex),
            proposal_id=str(payload.get("proposal_id", "") or ""),
            project_id=str(payload.get("project_id", "") or "default"),
            event_kind=event_kind,
            summary=str(payload.get("summary", "") or ""),
            details=dict(payload.get("details", {}) or {}),
            created_at=datetime.fromisoformat(created) if created else datetime.now(UTC),
        )


@dataclass
class _ReorgApprovalTask:
    """approval 桥接用的轻量 task-like 对象（Bandi 无 Task 模型）。"""

    id: str
    session_id: str | None
    project_id: str
    title: str
    description: str
    assigned_to: str
    metadata: dict[str, Any]


def infer_scope(changeset: ReorgChangeSet) -> ReorgScope:
    """存在角色变更 → org_mutation，否则 task_adjustment。"""
    if changeset.role_changes:
        return ReorgScope.ORG_MUTATION
    return ReorgScope.TASK_ADJUSTMENT


def classify_risk(scope: ReorgScope, changeset: ReorgChangeSet) -> ReorgRiskLevel:
    """org_mutation → HIGH；非法调整 action → ；否则 LOW。"""
    if scope == ReorgScope.ORG_MUTATION:
        return ReorgRiskLevel.HIGH
    for adjustment in changeset.task_adjustments:
        if adjustment.action not in {
            "reassign",
            "reprioritize",
            "update_description",
            "append_acceptance_criteria",
            "request_review",
        }:
            return ReorgRiskLevel.MEDIUM
    return ReorgRiskLevel.LOW


def normalize_changeset(changeset: ReorgChangeSet) -> ReorgChangeSet:
    """将 dict 元素规整为数据类元素。"""
    role_changes = [
        item if isinstance(item, ReorgRoleChange) else ReorgRoleChange.from_dict(item)
        for item in changeset.role_changes
    ]
    task_adjustments = [
        item if isinstance(item, ReorgTaskAdjustment) else ReorgTaskAdjustment.from_dict(item)
        for item in changeset.task_adjustments
    ]
    return ReorgChangeSet(
        role_changes=role_changes,
        task_adjustments=task_adjustments,
        metadata=dict(changeset.metadata),
    )
