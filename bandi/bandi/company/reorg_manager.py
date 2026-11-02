# 功能：组织重构编排——ReorgManager 提出/审批/应用/迁移（类型与纯函数见 reorg_types.py）

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from bandi.company.approval import ApprovalEngine
from bandi.company.communication import CommunicationManager
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.reorg_types import (
    ACTIVE_PHASES,
    OrgSnapshot,
    ReorgChangeSet,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgMigrationPlan,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgRoleChange,  # noqa: F401 - re-export
    ReorgScope,
    ReorgTaskAdjustment,  # noqa: F401 - re-export
    _ReorgApprovalTask,
    classify_risk,
    infer_scope,
    normalize_changeset,
    validate_reorg_proposal_transition,
)
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem
from bandi.company.work_item_identity import work_item_identity_payload_for_task


class ReorgManager:
    """协调运行时组织变更的提出、审批、应用与迁移（同步实现）。"""

    ACTIVE_PHASES = ACTIVE_PHASES

    def __init__(
        self,
        store: WorkItemStore,
        org_engine: OrgEngine,
        approval_engine: ApprovalEngine | None = None,
        communication: CommunicationManager | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.org_engine = org_engine
        self.approval_engine = approval_engine
        self.communication = communication
        self.progress_callback = progress_callback

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def build_org_snapshot(self, project_id: str = "default") -> OrgSnapshot:
        tasks = self.store.list_all()
        active_tasks = [
            {
                "task_id": item.id,
                "title": item.title,
                "status": item.phase.value,
                "assigned_to": item.assignee_role,
                **work_item_identity_payload_for_task(item),
                "org_version": item.metadata.get(
                    "org_version", self.org_engine.current_org_version()
                ),
                "runtime_topology_version": item.metadata.get(
                    "runtime_topology_version",
                    self.org_engine.current_runtime_topology_version(),
                ),
            }
            for item in tasks
            if item.phase in self.ACTIVE_PHASES | {Phase.RUNNING}
        ]
        return self.org_engine.snapshot_org(project_id=project_id, active_tasks=active_tasks)

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
        if isinstance(changeset, dict):
            changeset = ReorgChangeSet.from_dict(changeset)
        changeset = normalize_changeset(changeset or ReorgChangeSet())
        scope = scope or infer_scope(changeset)
        risk_level = classify_risk(scope, changeset)
        snapshot = self.build_org_snapshot(project_id)
        self.store.save_org_snapshot(snapshot)

        migration_plan = self._build_migration_plan(
            project_id=project_id,
            changeset=changeset,
            snapshot=snapshot,
            target_org_version=snapshot.org_version
            + (1 if scope == ReorgScope.ORG_MUTATION else 0),
        )

        proposal = ReorgProposal(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            scope=scope,
            risk_level=risk_level,
            status=ReorgProposalStatus.PROPOSED,
            title=title or summary[:120],
            summary=summary,
            rationale=rationale or summary,
            user_confirmation_required=(
                scope != ReorgScope.TASK_ADJUSTMENT or risk_level != ReorgRiskLevel.LOW
            ),
            old_org_version=snapshot.org_version,
            new_org_version=migration_plan.metadata.get("target_org_version", snapshot.org_version),
            old_runtime_topology_version=snapshot.runtime_topology_version,
            new_runtime_topology_version=snapshot.runtime_topology_version,
            changeset=changeset,
            migration_plan=migration_plan,
            impact_summary={
                "affected_tasks": len(migration_plan.affected_task_ids),
                "affected_checkpoints": len(migration_plan.affected_checkpoint_ids),
                "affected_employees": len(migration_plan.affected_employee_ids),
                "role_mapping": migration_plan.role_mapping,
            },
            metadata=dict(metadata or {}),
        )
        self.store.save_reorg_proposal(proposal)
        self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=project_id,
                event_kind=ReorgEventKind.PROPOSED,
                summary=proposal.summary,
                details={
                    "scope": proposal.scope.value,
                    "risk_level": proposal.risk_level.value,
                    "changeset": proposal.changeset.to_dict(),
                },
            )
        )
        return proposal

    async def request_reorg_approval(self, proposal_id: str) -> tuple[bool, ReorgProposal]:
        proposal = self._require_proposal(proposal_id)
        validate_reorg_proposal_transition(proposal.status, ReorgProposalStatus.APPROVED)
        if not proposal.user_confirmation_required:
            proposal.status = ReorgProposalStatus.APPROVED
            proposal.updated_at = datetime.now(UTC)
            self.store.save_reorg_proposal(proposal)
            return True, proposal
        if not self.approval_engine:
            raise RuntimeError("Approval engine is unavailable")

        approval_task = _ReorgApprovalTask(
            id=proposal.task_id or proposal.proposal_id,
            session_id=proposal.session_id,
            project_id=proposal.project_id,
            title=proposal.title,
            description=proposal.summary,
            assigned_to=proposal.source_role_id or "coordinator",
            metadata={
                "reorg_proposal_id": proposal.proposal_id,
                "org_version": proposal.old_org_version,
                "runtime_topology_version": proposal.old_runtime_topology_version,
            },
        )
        approved, decision = await self.approval_engine.authorize_work_item_action(
            task=approval_task,
            work_item_title=f"reorg:{proposal.title or proposal.proposal_id}",
            metadata={
                "role_id": proposal.source_role_id or "owner",
                "company_profile": self.org_engine.get_company_profile(),
                "gate_type": "company_reorg",
                "proposal_id": proposal.proposal_id,
                "scope": proposal.scope.value,
                "risk_level": proposal.risk_level.value,
            },
            on_progress=self.progress_callback,
            force_human=True,
        )
        proposal.approval_notes = decision.reason
        proposal.status = ReorgProposalStatus.APPROVED if approved else ReorgProposalStatus.DENIED
        proposal.updated_at = datetime.now(UTC)
        self.store.save_reorg_proposal(proposal)
        self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPROVED if approved else ReorgEventKind.DENIED,
                summary=decision.reason,
                details={
                    "approval_action": decision.action.value,
                    "risk_level": decision.risk_level.value,
                    "reason": decision.reason,
                },
            )
        )
        return approved, proposal

    def set_reorg_approval(
        self, proposal_id: str, approved: bool, notes: str = "", source: str = "manual"
    ) -> ReorgProposal:
        proposal = self._require_proposal(proposal_id)
        target = ReorgProposalStatus.APPROVED if approved else ReorgProposalStatus.DENIED
        validate_reorg_proposal_transition(proposal.status, target)
        proposal.status = target
        proposal.approval_notes = notes or proposal.approval_notes
        proposal.updated_at = datetime.now(UTC)
        self.store.save_reorg_proposal(proposal)
        self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPROVED if approved else ReorgEventKind.DENIED,
                summary=notes or proposal.summary,
                details={"status": proposal.status.value, "source": source},
            )
        )
        return proposal

    def apply_reorg(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._require_proposal(proposal_id)
        if proposal.status == ReorgProposalStatus.APPLIED:
            raise ValueError("Proposal has already been applied.")
        if proposal.status != ReorgProposalStatus.APPROVED:
            raise ValueError("Proposal must be approved before apply.")

        before_snapshot = self.build_org_snapshot(proposal.project_id)
        self.store.save_org_snapshot(before_snapshot)
        proposal.migration_plan.rollback_snapshot_id = before_snapshot.snapshot_id

        if proposal.scope == ReorgScope.ORG_MUTATION or proposal.changeset.role_changes:
            change_result = self.org_engine.apply_changeset(
                proposal.changeset,
                persist=True,
            )
        else:
            change_result = {
                "old_org_version": self.org_engine.current_org_version(),
                "new_org_version": self.org_engine.current_org_version(),
                "role_mapping": {},
            }

        migration_summary = self._migrate_active_state(proposal, change_result)
        proposal.status = ReorgProposalStatus.APPLIED
        proposal.old_org_version = change_result["old_org_version"]
        proposal.new_org_version = change_result["new_org_version"]
        proposal.migration_plan.role_mapping = dict(change_result.get("role_mapping", {}))
        proposal.migration_plan.metadata["migration_summary"] = migration_summary
        proposal.updated_at = datetime.now(UTC)
        self.store.save_reorg_proposal(proposal)

        after_snapshot = self.build_org_snapshot(proposal.project_id)
        self.store.save_org_snapshot(after_snapshot)
        self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPLIED,
                summary=proposal.summary,
                details={
                    "change_result": change_result,
                    "migration_summary": migration_summary,
                    "snapshot_id": after_snapshot.snapshot_id,
                },
            )
        )
        return {
            "proposal_id": proposal.proposal_id,
            "status": proposal.status.value,
            "migration_summary": migration_summary,
            "change_result": change_result,
            "snapshot_id": after_snapshot.snapshot_id,
        }

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
        proposal = self.propose_reorg(
            project_id=project_id,
            summary=summary,
            rationale=summary,
            initiated_by=source_role_id,
            source_role_id=source_role_id,
            changeset=changeset,
            scope=ReorgScope.TASK_ADJUSTMENT,
            session_id=session_id,
            task_id=task_id,
            metadata={"auto_apply_candidate": True},
        )
        if proposal.risk_level == ReorgRiskLevel.LOW and self._is_top_level_role(source_role_id):
            self.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                notes="Auto-approved low-risk task adjustment.",
            )
            result = self.apply_reorg(proposal.proposal_id)
            self.store.record_reorg_event(
                ReorgEventRecord(
                    proposal_id=proposal.proposal_id,
                    project_id=proposal.project_id,
                    event_kind=ReorgEventKind.AUTO_TASK_ADJUSTED,
                    summary=summary,
                    details=result,
                )
            )
            return {"proposal": proposal, "auto_applied": True, "result": result}
        return {"proposal": proposal, "auto_applied": False}

    def _build_migration_plan(
        self,
        *,
        project_id: str,
        changeset: ReorgChangeSet,
        snapshot: OrgSnapshot,
        target_org_version: int,
    ) -> ReorgMigrationPlan:
        tasks = self.store.list_all()
        affected_tasks = [item.id for item in tasks if item.phase in self.ACTIVE_PHASES]
        role_mapping: dict[str, str] = {}
        for change in changeset.role_changes:
            if change.action == "replace":
                target = change.replacement_role_id or change.role.get("id") or ""
                if target:
                    role_mapping[change.role_id] = target
            elif change.action == "remove":
                role_mapping[change.role_id] = ""
        affected_employee_ids = [
            employee.employee_id
            for employee in self.org_engine.list_employees()
            if employee.role_id in role_mapping
        ]
        warnings: list[str] = []
        if any(item.phase == Phase.RUNNING for item in tasks):
            warnings.append(
                "Running tasks are not force-migrated and will continue until their current "
                "iteration completes."
            )
        return ReorgMigrationPlan(
            affected_task_ids=affected_tasks,
            affected_checkpoint_ids=[],
            affected_handoff_ids=[],
            affected_employee_ids=affected_employee_ids,
            role_mapping=role_mapping,
            invalidated_waits=[],
            migration_notes=[
                f"Snapshot org_version={snapshot.org_version}.",
                f"Target org_version={target_org_version}.",
            ],
            compatibility_warnings=warnings,
            metadata={
                "target_org_version": target_org_version,
            },
        )

    def _migrate_active_state(
        self, proposal: ReorgProposal, change_result: dict[str, Any]
    ) -> dict[str, Any]:
        tasks = self.store.list_all()
        migrated_task_ids: list[str] = []
        role_mapping = dict(change_result.get("role_mapping", {}))
        target_org_version = change_result.get(
            "new_org_version", self.org_engine.current_org_version()
        )

        for item in tasks:
            if item.phase == Phase.RUNNING:
                item.metadata = dict(item.metadata)
                item.metadata["migration_status"] = "pending_running_completion"
                item.metadata["reorg_proposal_id"] = proposal.proposal_id
                self.store.save(item)
                continue
            if item.phase not in self.ACTIVE_PHASES:
                continue
            item.metadata = dict(item.metadata)
            item.runtime_metadata = dict(item.runtime_metadata)
            current_role = item.assignee_role or str(item.metadata.get("work_item_role_id", ""))
            new_role = role_mapping.get(current_role, current_role)
            if current_role and new_role and new_role != current_role:
                item.assignee_role = new_role
                item.metadata["work_item_role_id"] = new_role
            elif current_role and new_role == "":
                item.phase = Phase.CANCELLED
            self._apply_task_adjustments(item, proposal.changeset)
            peer_wait = dict(item.metadata.get("peer_wait", {}))
            if peer_wait:
                waiting_on = list(peer_wait.get("waiting_on_agents", []))
                peer_wait["waiting_on_agents"] = [
                    role_mapping.get(agent_id, agent_id)
                    for agent_id in waiting_on
                    if role_mapping.get(agent_id, agent_id)
                ]
                item.metadata["peer_wait"] = peer_wait
            active_meeting = dict(item.runtime_metadata.get("active_meeting", {}))
            if active_meeting:
                participants = list(active_meeting.get("participants", []))
                if participants:
                    active_meeting["participants"] = [
                        role_mapping.get(agent_id, agent_id)
                        for agent_id in participants
                        if role_mapping.get(agent_id, agent_id)
                    ]
                    item.runtime_metadata["active_meeting"] = active_meeting
            item.metadata["org_version"] = target_org_version
            item.metadata["reorg_proposal_id"] = proposal.proposal_id
            item.metadata["migration_status"] = "migrated"
            item.metadata["superseded_by_reorg"] = proposal.proposal_id
            item.runtime_metadata["migration_reason"] = proposal.summary
            item.runtime_metadata["migration_role_mapping"] = role_mapping
            item.runtime_metadata["migration_handoff"] = {
                "proposal_id": proposal.proposal_id,
                "reason": proposal.summary,
                "previous_role": current_role,
                "current_role": item.assignee_role,
            }
            self.store.save(item)
            migrated_task_ids.append(item.id)

        migrated_employee_ids, retired_employee_ids = self.org_engine.reallocate_employee_roles(
            role_mapping
        )
        proposal.migration_plan.affected_task_ids = migrated_task_ids
        proposal.migration_plan.affected_checkpoint_ids = []
        proposal.migration_plan.affected_employee_ids = migrated_employee_ids + retired_employee_ids
        proposal.migration_plan.metadata["target_org_version"] = target_org_version
        self._emit_progress(
            f"[Reorg] Applied proposal {proposal.proposal_id}: migrated "
            f"{len(migrated_task_ids)} tasks, migrated {len(migrated_employee_ids)} "
            f"employees, retired {len(retired_employee_ids)} employees."
        )
        return {
            "migrated_task_ids": migrated_task_ids,
            "migrated_checkpoint_ids": [],
            "migrated_employee_ids": migrated_employee_ids,
            "retired_employee_ids": retired_employee_ids,
            "target_org_version": target_org_version,
        }

    def _apply_task_adjustments(self, item: WorkItem, changeset: ReorgChangeSet) -> None:
        if not changeset.task_adjustments:
            return
        for adjustment in changeset.task_adjustments:
            if adjustment.task_id != item.id:
                continue
            if adjustment.action == "reassign" and adjustment.new_role_id:
                item.assignee_role = adjustment.new_role_id
                item.metadata["work_item_role_id"] = adjustment.new_role_id
            elif adjustment.action == "reprioritize" and adjustment.priority is not None:
                item.metadata["priority"] = adjustment.priority
            elif (
                adjustment.action == "update_description" and adjustment.description_append.strip()
            ):
                addition = adjustment.description_append.strip()
                if addition not in item.description:
                    item.description = f"{item.description}\n\nAdjustment note:\n{addition}".strip()
            elif (
                adjustment.action == "append_acceptance_criteria" and adjustment.acceptance_criteria
            ):
                criteria = list(item.metadata.get("acceptance_criteria", []))
                for item_criterion in adjustment.acceptance_criteria:
                    if item_criterion not in criteria:
                        criteria.append(item_criterion)
                item.metadata["acceptance_criteria"] = criteria
            elif adjustment.action == "request_review":
                item.metadata["force_additional_review"] = True

    def _is_top_level_role(self, role_id: str) -> bool:
        agent = self.org_engine.get_agent(role_id)
        return agent is not None and agent.reports_to is None

    def _require_proposal(self, proposal_id: str) -> ReorgProposal:
        proposal = self.store.get_reorg_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Unknown reorg proposal `{proposal_id}`.")
        return proposal
