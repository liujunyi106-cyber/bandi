from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bandi.company.approval import ApprovalAction, ApprovalEngine
from bandi.company.org_engine import OrgEmployee, OrgEngine, OrgRole
from bandi.company.phase import Phase
from bandi.company.reorg_manager import (
    ACTIVE_PHASES,
    OrgSnapshot,
    ReorgChangeSet,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgManager,
    ReorgMigrationPlan,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgRoleChange,
    ReorgScope,
    ReorgTaskAdjustment,
    classify_risk,
    infer_scope,
    normalize_changeset,
)
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


class _YesUI:
    async def ask_confirmation(self, prompt: str) -> bool:
        return True


class _NoUI:
    async def ask_confirmation(self, prompt: str) -> bool:
        return False


class TestReorgDataModels:
    def test_reorg_role_change_roundtrip(self):
        change = ReorgRoleChange(
            action="replace",
            role_id="engineer",
            replacement_role_id="senior_engineer",
            role={"id": "senior_engineer", "name": "Senior Engineer"},
            reason="promotion",
        )
        loaded = ReorgRoleChange.from_dict(change.to_dict())
        assert loaded == change

    def test_reorg_task_adjustment_roundtrip(self):
        adj = ReorgTaskAdjustment(
            task_id="t1",
            action="append_acceptance_criteria",
            acceptance_criteria=["c1", "c2"],
        )
        loaded = ReorgTaskAdjustment.from_dict(adj.to_dict())
        assert loaded == adj

    def test_reorg_change_set_roundtrip_with_nested_objects(self):
        changeset = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="remove", role_id="viewer")],
            task_adjustments=[ReorgTaskAdjustment(task_id="t1", action="request_review")],
            metadata={"origin": "test"},
        )
        loaded = ReorgChangeSet.from_dict(changeset.to_dict())
        assert loaded == changeset
        assert loaded.role_changes[0].action == "remove"

    def test_reorg_migration_plan_roundtrip(self):
        plan = ReorgMigrationPlan(
            affected_task_ids=["a", "b"],
            role_mapping={"engineer": "senior_engineer"},
            compatibility_warnings=["running tasks not migrated"],
            metadata={"target_org_version": 2},
        )
        loaded = ReorgMigrationPlan.from_dict(plan.to_dict())
        assert loaded == plan

    def test_org_snapshot_roundtrip(self):
        snapshot = OrgSnapshot(
            project_id="p1",
            org_version=3,
            roles=[{"id": "ceo", "name": "CEO"}],
            active_tasks=[{"task_id": "t1", "status": "running"}],
        )
        loaded = OrgSnapshot.from_dict(snapshot.to_dict())
        assert loaded.snapshot_id == snapshot.snapshot_id
        assert loaded.org_version == 3
        assert loaded.roles == [{"id": "ceo", "name": "CEO"}]
        assert loaded.created_at == snapshot.created_at

    def test_reorg_proposal_roundtrip(self):
        proposal = ReorgProposal(
            project_id="p1",
            scope=ReorgScope.ORG_MUTATION,
            risk_level=ReorgRiskLevel.HIGH,
            status=ReorgProposalStatus.PROPOSED,
            title="reorg",
            summary="restructure",
            user_confirmation_required=True,
            changeset=ReorgChangeSet(
                role_changes=[
                    ReorgRoleChange(action="add", replacement_role_id="pm", role={"id": "pm"})
                ]
            ),
            migration_plan=ReorgMigrationPlan(affected_task_ids=["t1"]),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        loaded = ReorgProposal.from_dict(proposal.to_dict())
        assert loaded == proposal
        assert loaded.changeset.role_changes[0].replacement_role_id == "pm"

    def test_reorg_event_record_roundtrip(self):
        event = ReorgEventRecord(
            proposal_id="p1",
            project_id="prj",
            event_kind=ReorgEventKind.APPLIED,
            summary="done",
            details={"ok": True},
        )
        loaded = ReorgEventRecord.from_dict(event.to_dict())
        assert loaded == event


class TestReorgPureFunctions:
    def test_infer_scope_org_mutation_with_role_changes(self):
        changeset = ReorgChangeSet(role_changes=[ReorgRoleChange(action="remove", role_id="x")])
        assert infer_scope(changeset) == ReorgScope.ORG_MUTATION

    def test_infer_scope_task_adjustment_without_role_changes(self):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="reprioritize")])
        assert infer_scope(changeset) == ReorgScope.TASK_ADJUSTMENT

    def test_classify_risk_org_mutation_high(self):
        changeset = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="add", replacement_role_id="y")]
        )
        assert classify_risk(ReorgScope.ORG_MUTATION, changeset) == ReorgRiskLevel.HIGH

    def test_classify_risk_valid_adjustments_low(self):
        changeset = ReorgChangeSet(
            task_adjustments=[
                ReorgTaskAdjustment(action="reassign"),
                ReorgTaskAdjustment(action="update_description"),
            ]
        )
        assert classify_risk(ReorgScope.TASK_ADJUSTMENT, changeset) == ReorgRiskLevel.LOW

    def test_classify_risk_unknown_adjustment_medium(self):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="explode")])
        assert classify_risk(ReorgScope.TASK_ADJUSTMENT, changeset) == ReorgRiskLevel.MEDIUM

    def test_normalize_changeset_coerces_dict_items(self):
        changeset = ReorgChangeSet(
            role_changes=[{"action": "remove", "role_id": "viewer"}],
            task_adjustments=[{"task_id": "t1", "action": "request_review"}],
        )
        normalized = normalize_changeset(changeset)
        assert isinstance(normalized.role_changes[0], ReorgRoleChange)
        assert isinstance(normalized.task_adjustments[0], ReorgTaskAdjustment)

    def test_act_ive_phases_exclude_terminal_and_running(self):
        assert Phase.RUNNING not in ACTIVE_PHASES
        assert Phase.APPROVED not in ACTIVE_PHASES
        assert Phase.CANCELLED not in ACTIVE_PHASES
        assert Phase.READY in ACTIVE_PHASES
        assert Phase.AWAITING_MANAGER_REVIEW in ACTIVE_PHASES


class TestReorgStore:
    def test_save_and_get_reorg_proposal(self, store: WorkItemStore):
        proposal = ReorgProposal(
            project_id="p1",
            scope=ReorgScope.TASK_ADJUSTMENT,
            risk_level=ReorgRiskLevel.LOW,
            status=ReorgProposalStatus.PROPOSED,
            summary="hi",
            changeset=ReorgChangeSet(
                task_adjustments=[
                    ReorgTaskAdjustment(task_id="t1", action="reprioritize", priority=1)
                ]
            ),
        )
        store.save_reorg_proposal(proposal)
        loaded = store.get_reorg_proposal(proposal.proposal_id)
        assert loaded is not None
        assert loaded == proposal
        assert loaded.changeset.task_adjustments[0].priority == 1

    def test_get_reorg_proposal_nonexistent(self, store: WorkItemStore):
        assert store.get_reorg_proposal("nope") is None

    def test_save_and_get_org_snapshot(self, store: WorkItemStore):
        snapshot = OrgSnapshot(project_id="p1", org_version=2, roles=[{"id": "ceo"}])
        store.save_org_snapshot(snapshot)
        loaded = store.get_org_snapshot(snapshot.snapshot_id)
        assert loaded is not None
        assert loaded.org_version == 2

    def test_get_latest_org_snapshot(self, store: WorkItemStore):
        s1 = OrgSnapshot(project_id="p1", org_version=1)
        s2 = OrgSnapshot(project_id="p1", org_version=3)
        store.save_org_snapshot(s1)
        store.save_org_snapshot(s2)
        latest = store.get_latest_org_snapshot("p1")
        assert latest is not None
        assert latest.org_version == 3

    def test_record_and_list_reorg_events(self, store: WorkItemStore):
        e1 = ReorgEventRecord(
            proposal_id="p1", project_id="prj", event_kind=ReorgEventKind.PROPOSED
        )
        e2 = ReorgEventRecord(proposal_id="p1", project_id="prj", event_kind=ReorgEventKind.APPLIED)
        store.record_reorg_event(e1)
        store.record_reorg_event(e2)
        events = store.list_reorg_events("prj", proposal_id="p1")
        assert len(events) == 2
        kinds = {e.event_kind for e in events}
        assert kinds == {ReorgEventKind.PROPOSED, ReorgEventKind.APPLIED}

    def test_list_reorg_proposals_filtered_by_status(self, store: WorkItemStore):
        a = ReorgProposal(project_id="p1", status=ReorgProposalStatus.PROPOSED)
        b = ReorgProposal(project_id="p1", status=ReorgProposalStatus.APPLIED)
        store.save_reorg_proposal(a)
        store.save_reorg_proposal(b)
        applied = store.list_reorg_proposals("p1", status="applied")
        assert len(applied) == 1
        assert applied[0].proposal_id == b.proposal_id


class TestOrgEngineReorg:
    def test_current_versions_default_to_one(self):
        engine = OrgEngine()
        assert engine.current_org_version() == 1
        assert engine.current_runtime_topology_version() == 1

    def test_snapshot_org_contains_roles_and_versions(self):
        engine = OrgEngine()
        snapshot = engine.snapshot_org(project_id="p1")
        assert snapshot.org_version == 1
        assert snapshot.company_profile == "corporate"
        assert len(snapshot.roles) == 9

    def test_apply_changeset_add_role_bumps_version(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="add", replacement_role_id="pm", role={"id": "pm"})
            ]
        )
        result = engine.apply_changeset(changeset)
        assert result["old_org_version"] == 1
        assert result["new_org_version"] == 2
        assert engine.get_agent("pm") is not None
        assert engine.get_company_profile() == "custom"

    def test_apply_changeset_remove_role_builds_mapping(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="remove", role_id="viewer", replacement_role_id="analyst")
            ]
        )
        result = engine.apply_changeset(changeset)
        assert result["role_mapping"] == {"viewer": "analyst"}
        assert engine.get_agent("viewer") is None

    def test_apply_changeset_replace_role(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer", "name": "Senior Engineer", "reports_to": "em"},
                )
            ]
        )
        result = engine.apply_changeset(changeset)
        assert result["role_mapping"] == {"engineer": "senior_engineer"}
        assert engine.get_agent("engineer") is None
        assert engine.get_agent("senior_engineer") is not None

    def test_apply_changeset_update_role(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="update", role_id="engineer", role={"name": "IC Engineer"})
            ]
        )
        engine.apply_changeset(changeset)
        assert engine.get_agent("engineer").name == "IC Engineer"

    def test_validate_changeset_unknown_role(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(role_changes=[ReorgRoleChange(action="remove", role_id="ghost")])
        errors = engine.validate_changeset(changeset)
        assert any("Unknown role" in e for e in errors)

    def test_validate_changeset_missing_target_id(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(role_changes=[ReorgRoleChange(action="add", role={})])
        errors = engine.validate_changeset(changeset)
        assert any("requires a new role id" in e for e in errors)

    def test_apply_changeset_rejects_invalid(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(role_changes=[ReorgRoleChange(action="remove", role_id="ghost")])
        with pytest.raises(ValueError, match="Unknown role"):
            engine.apply_changeset(changeset)

    def test_apply_changeset_task_adjustment_only_no_role_changes(self):
        engine = OrgEngine()
        changeset = ReorgChangeSet(
            task_adjustments=[ReorgTaskAdjustment(task_id="t1", action="request_review")]
        )
        result = engine.apply_changeset(changeset)
        assert result["role_mapping"] == {}
        assert result["new_org_version"] == 2

    def test_standalone_role_helpers(self):
        engine = OrgEngine()
        payload = {"id": "pm", "name": "PM", "reports_to": "ceo", "can_spawn": ["engineer"]}
        role = engine._role_from_dict(payload)
        assert isinstance(role, OrgRole)
        assert role.id == "pm"
        assert engine._role_to_dict(role)["can_spawn"] == ["engineer"]


class TestApprovalWorkItemAction:
    @pytest.mark.asyncio
    async def test_low_risk_auto_approve_without_ui(self):
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "low", "gate_type": "company_reorg"},
        )
        assert approved is True
        assert decision.action == ApprovalAction.AUTO_APPROVE

    @pytest.mark.asyncio
    async def test_medium_risk_requires_confirm_without_ui(self):
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "medium"},
        )
        assert approved is False
        assert decision.action == ApprovalAction.REQUIRE_CONFIRM

    @pytest.mark.asyncio
    async def test_force_human_with_yes_ui(self):
        engine = ApprovalEngine(user_interaction=_YesUI())
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "high"},
            force_human=True,
        )
        assert approved is True
        assert decision.action == ApprovalAction.AUTO_APPROVE

    @pytest.mark.asyncio
    async def test_force_human_with_no_ui(self):
        engine = ApprovalEngine(user_interaction=_NoUI())
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "high"},
            force_human=True,
        )
        assert approved is False
        assert decision.action == ApprovalAction.DENY

    @pytest.mark.asyncio
    async def test_force_human_without_ui_denied(self):
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "medium"},
            force_human=True,
        )
        assert approved is False
        assert decision.action == ApprovalAction.REQUIRE_CONFIRM

    @pytest.mark.asyncio
    async def test_unknown_risk_defaults_medium(self):
        engine = ApprovalEngine()
        approved, _ = await engine.authorize_work_item_action(
            task=None,
            work_item_title="reorg:x",
            metadata={"risk_level": "banana"},
        )
        assert approved is False


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_reorg.db")
    return WorkItemStore(db_path)


@pytest.fixture
def manager(store):
    return ReorgManager(store=store, org_engine=OrgEngine())


class TestReorgManagerPropose:
    def test_propose_task_adjustment_low_risk_no_confirmation(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[ReorgTaskAdjustment(task_id=item.id, action="request_review")]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="adjust", changeset=changeset)
        assert proposal.scope == ReorgScope.TASK_ADJUSTMENT
        assert proposal.risk_level == ReorgRiskLevel.LOW
        assert proposal.user_confirmation_required is False
        assert proposal.status == ReorgProposalStatus.PROPOSED
        assert proposal.migration_plan.affected_task_ids == [item.id]

    def test_propose_org_mutation_high_risk_confirmation(self, manager, store):
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="add", replacement_role_id="pm", role={"id": "pm"})
            ]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="org", changeset=changeset)
        assert proposal.scope == ReorgScope.ORG_MUTATION
        assert proposal.risk_level == ReorgRiskLevel.HIGH
        assert proposal.user_confirmation_required is True

    def test_propose_records_snapshot_and_event(self, manager, store):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        manager.propose_reorg(project_id="p1", summary="x", changeset=changeset)
        assert store.get_latest_org_snapshot("p1") is not None
        assert len(store.list_reorg_events("p1")) == 1

    def test_propose_accepts_dict_changeset(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="x",
            changeset={"task_adjustments": [{"action": "request_review"}]},
        )
        assert isinstance(proposal.changeset, ReorgChangeSet)
        assert proposal.changeset.task_adjustments[0].action == "request_review"

    @pytest.mark.asyncio
    async def test_propose_unknown_proposal_raises(self, manager):
        with pytest.raises(ValueError, match="Unknown reorg proposal"):
            await manager.request_reorg_approval("ghost")

    @pytest.mark.asyncio
    async def test_request_approval_auto_approves_when_no_confirmation_needed(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="low",
            changeset={"task_adjustments": [{"action": "request_review"}]},
        )
        approved, updated = await manager.request_reorg_approval(proposal.proposal_id)
        assert approved is True
        assert updated.status == ReorgProposalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_request_approval_with_yes_ui(self, store):
        manager = ReorgManager(
            store=store,
            org_engine=OrgEngine(),
            approval_engine=ApprovalEngine(user_interaction=_YesUI()),
        )
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="org",
            changeset={
                "role_changes": [
                    {"action": "add", "replacement_role_id": "pm", "role": {"id": "pm"}}
                ]
            },
        )
        approved, updated = await manager.request_reorg_approval(proposal.proposal_id)
        assert approved is True
        assert updated.status == ReorgProposalStatus.APPROVED
        assert updated.approval_notes

    @pytest.mark.asyncio
    async def test_request_approval_without_engine_raises(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="org",
            changeset={
                "role_changes": [
                    {"action": "add", "replacement_role_id": "pm", "role": {"id": "pm"}}
                ]
            },
        )
        with pytest.raises(RuntimeError, match="Approval engine is unavailable"):
            await manager.request_reorg_approval(proposal.proposal_id)

    def test_set_reorg_approval_denied(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="org",
            changeset={
                "role_changes": [
                    {"action": "add", "replacement_role_id": "pm", "role": {"id": "pm"}}
                ]
            },
        )
        updated = manager.set_reorg_approval(proposal.proposal_id, approved=False, notes="no")
        assert updated.status == ReorgProposalStatus.DENIED


class TestReorgManagerApply:
    def _approved_proposal(self, manager, store, changeset: ReorgChangeSet) -> ReorgProposal:
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="ok")
        return manager.store.get_reorg_proposal(proposal.proposal_id)

    def test_apply_requires_approval(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="org",
            changeset={
                "role_changes": [
                    {"action": "add", "replacement_role_id": "pm", "role": {"id": "pm"}}
                ]
            },
        )
        with pytest.raises(ValueError, match="must be approved"):
            manager.apply_reorg(proposal.proposal_id)

    def test_apply_denied_proposal_rejected(self, manager):
        proposal = manager.propose_reorg(
            project_id="p1",
            summary="org",
            changeset={
                "role_changes": [
                    {"action": "add", "replacement_role_id": "pm", "role": {"id": "pm"}}
                ]
            },
        )
        manager.set_reorg_approval(proposal.proposal_id, approved=False, notes="no")
        with pytest.raises(ValueError, match="must be approved"):
            manager.apply_reorg(proposal.proposal_id)

    def test_apply_applied_proposal_rejected(self, manager, store):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        with pytest.raises(ValueError, match="already"):
            manager.apply_reorg(proposal.proposal_id)

    def test_apply_org_mutation_migrates_tasks(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer", "name": "Senior", "reports_to": "em"},
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        result = manager.apply_reorg(proposal.proposal_id)
        assert result["status"] == "applied"
        assert result["migration_summary"]["migrated_task_ids"] == [item.id]
        migrated = store.get(item.id)
        assert migrated.assignee_role == "senior_engineer"
        assert migrated.metadata["work_item_role_id"] == "senior_engineer"
        assert migrated.metadata["migration_status"] == "migrated"
        assert migrated.metadata["org_version"] == 2
        assert manager.org_engine.get_agent("senior_engineer") is not None
        assert manager.org_engine.get_agent("engineer") is None

    def test_apply_running_task_not_migrated(self, manager, store):
        item = create_work_item(
            title="running",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="engineer",
            phase=Phase.RUNNING,
        )
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.assignee_role == "engineer"
        assert migrated.metadata["migration_status"] == "pending_running_completion"
        assert migrated.metadata["reorg_proposal_id"] == proposal.proposal_id

    def test_apply_terminal_task_skipped(self, manager, store):
        item = create_work_item(
            title="done",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="engineer",
            phase=Phase.APPROVED,
        )
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.metadata.get("migration_status") is None

    def test_apply_removed_role_cancels_task(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="viewer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="remove", role_id="viewer")]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.phase == Phase.CANCELLED

    def test_apply_peer_wait_remapping(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        item.metadata["peer_wait"] = {"waiting_on_agents": ["engineer"]}
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.metadata["peer_wait"]["waiting_on_agents"] == ["senior_engineer"]

    def test_apply_active_meeting_remapping(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        item.runtime_metadata["active_meeting"] = {"participants": ["engineer", "pm"]}
        store.save(item)
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.runtime_metadata["active_meeting"]["participants"] == [
            "senior_engineer",
            "pm",
        ]

    def test_apply_task_adjustment_reassign(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[
                ReorgTaskAdjustment(task_id=item.id, action="reassign", new_role_id="architect")
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.assignee_role == "architect"

    def test_apply_task_adjustment_without_task_id_not_global(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[ReorgTaskAdjustment(action="reassign", new_role_id="architect")]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.assignee_role == "engineer"

    def test_apply_task_adjustment_update_description(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[
                ReorgTaskAdjustment(
                    task_id=item.id, action="update_description", description_append="extra"
                )
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert "extra" in migrated.description

    def test_apply_task_adjustment_reprioritize(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[
                ReorgTaskAdjustment(task_id=item.id, action="reprioritize", priority=1)
            ]
        )
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        migrated = store.get(item.id)
        assert migrated.metadata["priority"] == 1

    def test_apply_records_before_after_snapshots(self, manager, store):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        proposal = self._approved_proposal(manager, store, changeset)
        manager.apply_reorg(proposal.proposal_id)
        events = store.list_reorg_events("p1", proposal_id=proposal.proposal_id)
        kinds = {e.event_kind for e in events}
        assert ReorgEventKind.APPLIED in kinds
        applied = manager.store.get_reorg_proposal(proposal.proposal_id)
        assert applied.migration_plan.rollback_snapshot_id is not None

    def test_suggest_task_adjustment_top_level_low_risk_auto_applied(self, manager, store):
        item = create_work_item(
            title="t1", turn_type=WorkItemTurnType.EXECUTE, assignee_role="engineer"
        )
        store.save(item)
        changeset = ReorgChangeSet(
            task_adjustments=[ReorgTaskAdjustment(task_id=item.id, action="request_review")]
        )
        result = manager.suggest_task_adjustment(
            project_id="p1",
            source_role_id="ceo",
            summary="suggest",
            changeset=changeset,
        )
        assert result["auto_applied"] is True
        migrated = store.get(item.id)
        assert migrated.metadata.get("force_additional_review") is True

    def test_suggest_task_adjustment_unknown_role_not_auto_applied(self, manager):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        result = manager.suggest_task_adjustment(
            project_id="p1",
            source_role_id="ghost",
            summary="suggest",
            changeset=changeset,
        )
        assert result["auto_applied"] is False
        assert result["proposal"].status == ReorgProposalStatus.PROPOSED

    def test_suggest_task_adjustment_unverifiable_role_not_auto_applied(self, manager):
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        result = manager.suggest_task_adjustment(
            project_id="p1",
            source_role_id="owner",
            summary="suggest",
            changeset=changeset,
        )
        assert result["auto_applied"] is False
        assert result["proposal"].status == ReorgProposalStatus.PROPOSED


class TestReorgManagerInternals:
    def test_is_top_level_role(self, manager):
        assert manager._is_top_level_role("ceo") is True
        assert manager._is_top_level_role("engineer") is False
        assert manager._is_top_level_role("owner") is False

    def test_progress_callback_called(self, store):
        messages = []
        manager = ReorgManager(
            store=store,
            org_engine=OrgEngine(),
            progress_callback=messages.append,
        )
        changeset = ReorgChangeSet(task_adjustments=[ReorgTaskAdjustment(action="request_review")])
        proposal = manager.propose_reorg(project_id="p1", summary="x", changeset=changeset)
        manager.set_reorg_approval(proposal.proposal_id, approved=True)
        manager.apply_reorg(proposal.proposal_id)
        assert any("[Reorg]" in m for m in messages)


class TestReorgEmployeeReallocation:
    def test_migration_plan_lists_affected_employees(self, manager):
        engine = manager.org_engine
        engine.register_employee(OrgEmployee(employee_id="e1", name="E1", role_id="engineer"))
        engine.register_employee(OrgEmployee(employee_id="e2", name="E2", role_id="viewer"))
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        assert proposal.migration_plan.affected_employee_ids == ["e1"]
        assert proposal.impact_summary["affected_employees"] == 1

    def test_apply_reorg_migrates_employee_role(self, manager, store):
        engine = manager.org_engine
        engine.register_employee(OrgEmployee(employee_id="e1", name="E1", role_id="engineer"))
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    replacement_role_id="senior_engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="ok")
        result = manager.apply_reorg(proposal.proposal_id)
        assert result["migration_summary"]["migrated_employee_ids"] == ["e1"]
        employee = engine.get_employee("e1")
        assert employee.role_id == "senior_engineer"
        assert employee.status == "active"

    def test_apply_replace_with_role_id_only_migrates_employee(self, manager, store):
        engine = manager.org_engine
        engine.register_employee(OrgEmployee(employee_id="e1", name="E1", role_id="engineer"))
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="engineer",
                    role={"id": "senior_engineer"},
                )
            ]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        assert proposal.migration_plan.affected_employee_ids == ["e1"]
        assert proposal.impact_summary["affected_employees"] == 1
        manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="ok")
        result = manager.apply_reorg(proposal.proposal_id)
        assert result["migration_summary"]["migrated_employee_ids"] == ["e1"]
        employee = engine.get_employee("e1")
        assert employee.role_id == "senior_engineer"
        assert employee.status == "active"

    def test_apply_reorg_retires_employee_on_role_remove(self, manager, store):
        engine = manager.org_engine
        engine.register_employee(OrgEmployee(employee_id="e2", name="E2", role_id="viewer"))
        changeset = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="remove", role_id="viewer")]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="ok")
        result = manager.apply_reorg(proposal.proposal_id)
        assert result["migration_summary"]["retired_employee_ids"] == ["e2"]
        employee = engine.get_employee("e2")
        assert employee.status == "retired"
        assert employee.role_id == ""

    def test_migration_plan_roundtrip_with_employee_ids(self):
        plan = ReorgMigrationPlan(
            affected_employee_ids=["e1", "e2"],
            affected_task_ids=["t1"],
            metadata={"target_org_version": 2},
        )
        loaded = ReorgMigrationPlan.from_dict(plan.to_dict())
        assert loaded.affected_employee_ids == ["e1", "e2"]

    def test_apply_without_employees_pool_is_clean(self, manager):
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="add", replacement_role_id="pm", role={"id": "pm"})
            ]
        )
        proposal = manager.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="ok")
        result = manager.apply_reorg(proposal.proposal_id)
        assert result["migration_summary"].get("migrated_employee_ids") == []
        assert result["migration_summary"].get("retired_employee_ids") == []


def test_contract_reorg_proposal_applied_is_terminal(manager, store):
    changeset = ReorgChangeSet(
        role_changes=[ReorgRoleChange(action="add", role_id="x1", role={"id": "x1", "name": "X"})]
    )
    proposal = manager.propose_reorg(project_id="p1", summary="s", changeset=changeset)
    manager.set_reorg_approval(proposal.proposal_id, approved=True)
    manager.apply_reorg(proposal.proposal_id)

    with pytest.raises(ValueError):
        manager.set_reorg_approval(proposal.proposal_id, approved=False)


@pytest.mark.asyncio
async def test_contract_reorg_proposal_denied_cannot_reenter_approval(manager, store):
    changeset = ReorgChangeSet(
        role_changes=[ReorgRoleChange(action="add", role_id="x2", role={"id": "x2", "name": "X"})]
    )
    proposal = manager.propose_reorg(project_id="p1", summary="s", changeset=changeset)
    updated = manager.set_reorg_approval(proposal.proposal_id, approved=False)
    assert updated.status == ReorgProposalStatus.DENIED

    with pytest.raises(ValueError):
        await manager.request_reorg_approval(proposal.proposal_id)
