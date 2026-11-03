# 功能：元数据所有权——字段矩阵 spec 数据（静态声明，不含运行时逻辑）

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MetadataOwner(StrEnum):
    WORK_ITEM = "work_item"
    RUNTIME_TASK = "runtime_task"
    EXECUTION_COPY = "execution_copy"


@dataclass(frozen=True)
class MetadataFieldSpec:
    key: str
    owner: MetadataOwner
    allowed_locations: tuple[str, ...] | None = None
    legacy_fallback: bool = False
    migration_policy: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        # 默认位置与 _spec() 推导一致：WORK_ITEM 默认 work_item
        # RUNTIME_TASK 默认 task，避免绕过 _spec() 直接实例化时漂移。
        if self.allowed_locations is None:
            locations = ("work_item",) if self.owner == MetadataOwner.WORK_ITEM else ("task",)
        else:
            locations = self.allowed_locations
        object.__setattr__(self, "allowed_locations", tuple(locations))

    @property
    def allows_task_execution_copy(self) -> bool:
        return "task_execution_copy" in self.allowed_locations

    @property
    def allows_task_legacy_read(self) -> bool:
        return "task_legacy_read" in self.allowed_locations or self.legacy_fallback


@dataclass(frozen=True)
class MetadataOwnershipIssue:
    code: str
    severity: str
    key: str
    owner: str
    work_item_id: str = ""
    runtime_task_id: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _spec(
    key: str,
    owner: MetadataOwner,
    *,
    allowed_locations: Iterable[str] | None = None,
    legacy_fallback: bool = False,
    migration_policy: str = "",
    description: str = "",
) -> MetadataFieldSpec:
    return MetadataFieldSpec(
        key=key,
        owner=owner,
        allowed_locations=tuple(allowed_locations) if allowed_locations is not None else None,
        legacy_fallback=legacy_fallback,
        migration_policy=migration_policy,
        description=description,
    )


_WORK_ITEM_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec("phase", MetadataOwner.WORK_ITEM, description="Current phase state"),
    _spec("turn_type", MetadataOwner.WORK_ITEM, description="Current turn type"),
    _spec("assignee_role", MetadataOwner.WORK_ITEM, description="Assigned role"),
    _spec("parent_id", MetadataOwner.WORK_ITEM, description="Parent work item id"),
    _spec("children_ids", MetadataOwner.WORK_ITEM, description="Child work item ids"),
    _spec("title", MetadataOwner.WORK_ITEM, description="Work item title"),
    _spec("description", MetadataOwner.WORK_ITEM, description="Work item description"),
    _spec("links", MetadataOwner.WORK_ITEM, description="Entity links"),
    _spec(
        "work_kind",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "current_turn_mode",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "dependency_work_item_ids",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "waiting_on_work_item_ids",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "delegated_children_pending",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "handoff_context",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "context_preview",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "prompt_contract",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_prompt_contract",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_prompt_contract",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
    ),
    _spec(
        "prompt_contract_blocker",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_revision",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_action",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_reason",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_at",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_by_role_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_by_seat_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "manager_mutation_user_input",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "latest_user_directive",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "progress_log",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_legacy_read"),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "work_item_role_name",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "employee_assignment",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "employee_prompt_context",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "employee_delta_context",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "completion_report",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "deliverable_summary",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "work_item_summary",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "work_item_summary_for_downstream",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "work_item_artifact_index",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "verification_status",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "verification_evidence",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "verification",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "structured_review_verdict",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "review_owner_role_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_owner_seat_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_attempt",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_attempt_count",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_work_item_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_source_report_work_item_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_resolution",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_resolution_state",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_resolution_applied_work_item_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_worker_task_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_worker_role_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_worker_seat_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_completion_report",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_title",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_target_description",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_evidence",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "rework_feedback",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_feedback_version",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_rework_count",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_retry_hint",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_retry_of_attempt",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "review_retry_reason",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_attempt",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_attempt_count",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_work_item_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_worker_task_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_worker_role_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_worker_seat_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_title",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_target_description",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_source_summary",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_source_result_content",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "report_source_evidence",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "follow_up_actions",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "follow_up_action",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "follow_up_reason",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "follow_up_dedupe_key",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "synthesis_turn_started",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "synthesis_ready_at",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "synthesis_source_work_item_ids",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "synthesis_reports_to_role_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "synthesis_reports_to_seat_id",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        legacy_fallback=True,
        migration_policy="work_item_wins",
    ),
    _spec(
        "delivery_package",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "downstream_assignments",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "open_questions",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "assumptions",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "decisions",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "risks",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item",),
        legacy_fallback=True,
        migration_policy="backfill_if_missing",
    ),
    _spec(
        "ownership_contract",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
        description="Ownership contract: write_scope / allowed_collaboration_targets / downstream_consumer",
    ),
    _spec(
        "work_item_gate",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
        description="Gate contract: reviewer_role / rework / target projection ids",
    ),
    _spec(
        "execution_mode",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
        description="Execution mode marker (company_mode)",
    ),
    _spec(
        "handoff_log",
        MetadataOwner.WORK_ITEM,
        allowed_locations=("work_item", "task_execution_copy"),
        migration_policy="work_item_wins",
        description="Handoff history entries (from/to role pairs)",
    ),
)

_RUNTIME_TASK_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec("verdict", MetadataOwner.WORK_ITEM, description="Verifier verdict for the work item"),
    _spec("verdict_reason", MetadataOwner.WORK_ITEM, description="Verifier verdict explanation"),
    _spec("rework_count", MetadataOwner.WORK_ITEM, description="Number of rework cycles"),
    _spec("rework_reason", MetadataOwner.WORK_ITEM, description="Last rework trigger reason"),
    _spec(
        "escalation_level", MetadataOwner.WORK_ITEM, description="Escalation tier (peer/lead/owner)"
    ),
    _spec(
        "escalation_reason", MetadataOwner.WORK_ITEM, description="Escalation trigger explanation"
    ),
    _spec("approval_state", MetadataOwner.WORK_ITEM, description="Approval pipeline state"),
    _spec(
        "approval_decision", MetadataOwner.WORK_ITEM, description="Last approval decision record"
    ),
    _spec(
        "confirmation_policy",
        MetadataOwner.WORK_ITEM,
        description="Confirmation channel policy for this item",
    ),
    _spec("attempt_count", MetadataOwner.RUNTIME_TASK, description="Execution attempt counter"),
    _spec("last_attempt_id", MetadataOwner.RUNTIME_TASK, description="Most recent attempt id"),
    _spec("retry_count", MetadataOwner.RUNTIME_TASK, description="Retry counter within turn"),
    _spec(
        "retry_policy_name",
        MetadataOwner.RUNTIME_TASK,
        description="Applied retry policy identifier",
    ),
    _spec(
        "revision_count",
        MetadataOwner.RUNTIME_TASK,
        description="Revision counter (rework rounds against stagnation_threshold)",
    ),
    _spec("quality_score", MetadataOwner.WORK_ITEM, description="Review quality score"),
    _spec("quality_gate", MetadataOwner.WORK_ITEM, description="Quality gate threshold applied"),
    _spec("confidence_score", MetadataOwner.WORK_ITEM, description="Result confidence score"),
    _spec("deadline", MetadataOwner.WORK_ITEM, description="Item deadline timestamp"),
    _spec("priority", MetadataOwner.WORK_ITEM, description="Scheduling priority"),
    _spec(
        "priority_source",
        MetadataOwner.WORK_ITEM,
        description="Priority assignment origin (user/planner/SLA)",
    ),
    _spec("sla_breach_at", MetadataOwner.WORK_ITEM, description="SLA breach deadline"),
    _spec("last_feedback", MetadataOwner.WORK_ITEM, description="Latest reviewer/user feedback"),
    _spec("feedback_count", MetadataOwner.WORK_ITEM, description="Feedback round counter"),
    _spec(
        "validation_errors", MetadataOwner.RUNTIME_TASK, description="Last validation error list"
    ),
    _spec("schema_version", MetadataOwner.WORK_ITEM, description="Metadata schema version stamp"),
    _spec(
        "source_channel", MetadataOwner.WORK_ITEM, description="Inbound channel (api/cli/ws/thread)"
    ),
    _spec("thread_id", MetadataOwner.WORK_ITEM, description="Bound discussion thread id"),
    _spec(
        "origin_work_item_id",
        MetadataOwner.WORK_ITEM,
        description="Origin item for spawned children",
    ),
    _spec("tags", MetadataOwner.WORK_ITEM, description="Free-form classification tags"),
    _spec("owner_note", MetadataOwner.WORK_ITEM, description="Owner-visible note"),
    _spec(
        "session_id",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task_execution_copy", "task_legacy_read"),
        description="Runtime session id",
    ),
    _spec(
        "execution_log",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task_execution_copy",),
        description="Execution audit log",
    ),
    _spec(
        "tool_history",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task_execution_copy",),
        description="Tool call history",
    ),
    _spec(
        "error_info",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task_execution_copy",),
        description="Error information",
    ),
    _spec(
        "runtime_v2",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_verification",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_verification_evidence",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_verification_status",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "member_session_state",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "external_resume_session_id",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "external_resume_agent_type",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "working_memory",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "interrupted_recovery",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "last_stop_reason",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "peer_wait",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "comms_cross_role_history",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "comms_last_blocked_reactivation_at",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "comms_last_blocked_reactivation_key",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_control_state",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "automated_verification_results",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_session_team_instance_id",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_session_team_id",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
    _spec(
        "runtime_session_seat_id",
        MetadataOwner.RUNTIME_TASK,
        allowed_locations=("task",),
        migration_policy="task_only",
    ),
)

_EXECUTION_COPY_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec(
        "last_transition_reason",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy", "work_item"),
        description="Reason for last phase transition",
    ),
    _spec(
        "output_contract_result",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        description="Output contract validation result",
    ),
    _spec(
        "parent_session_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "mode",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "execution_model",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "runtime_model",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "original_message",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "company_profile",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_playbook",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "runtime_topology",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "organization_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "org_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_run_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_cell_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_team_instance_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_team_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_seat_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "delegation_role_session_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "work_item_role_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "seat_manager_role_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "manager_role_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "manager_seat_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "managed_team_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "seat_contact_role_ids",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "allowed_delegate_role_ids",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "force_native_execution",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "preferred_external_agent",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "selected_execution_agent",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "execution_agent_locked",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "selected_execution_agent_source",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "work_item_execution_strategy",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "adaptive",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "execution_task_ids",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "work_item_batch_id",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "target_output_dir",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "output_root",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "workspace_root",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "comms_workspace_root",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "comms_root",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "user_visible",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "authoritative_output",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "review_owner_kind",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "requires_user_feedback",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "feedback_scope",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "review_task",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "review_execution_work_item",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "report_execution_work_item",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
    _spec(
        "skip_work_item_sync",
        MetadataOwner.EXECUTION_COPY,
        allowed_locations=("task_execution_copy",),
        migration_policy="copy_from_runtime_context",
    ),
)

METADATA_FIELD_SPECS: dict[str, MetadataFieldSpec] = {
    spec.key: spec
    for spec in (
        *_WORK_ITEM_FIELDS,
        *_RUNTIME_TASK_FIELDS,
        *_EXECUTION_COPY_FIELDS,
    )
}

WORK_ITEM_OWNED_KEYS: frozenset[str] = frozenset(
    key for key, spec in METADATA_FIELD_SPECS.items() if spec.owner == MetadataOwner.WORK_ITEM
)
RUNTIME_TASK_OWNED_KEYS: frozenset[str] = frozenset(
    key for key, spec in METADATA_FIELD_SPECS.items() if spec.owner == MetadataOwner.RUNTIME_TASK
)
EXECUTION_COPY_KEYS: frozenset[str] = frozenset(
    key
    for key, spec in METADATA_FIELD_SPECS.items()
    if spec.owner == MetadataOwner.EXECUTION_COPY or spec.allows_task_execution_copy
)
# 命名说明：该集合并非「仅 execution_copy 拥有者」，而是「owner==EXECUTION_COPY
# 或 spec 允许 task 执行复制」的并集（含大量 WORK_ITEM owner 的 key）
# 供 copy_work_item_execution_metadata 挑选可复制字段。
_EXECUTION_COPY_KEYS_SORTED: tuple[str, ...] = tuple(sorted(EXECUTION_COPY_KEYS))
