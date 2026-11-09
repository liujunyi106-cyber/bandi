"""Reorg 管理器运行时装配接线测试（Dispatcher / Communication / CustomRuntime）。"""

import pytest

from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher
from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
from bandi.company.org_engine import OrgEngine
from bandi.company.recruiter import Recruiter
from bandi.company.reorg_manager import ReorgChangeSet, ReorgManager, ReorgRoleChange
from bandi.company.store import WorkItemStore


@pytest.fixture
def store(tmp_path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "reorg_wiring.db"))


class TestReorgDispatcherWiring:
    def test_dispatcher_assembles_reorg_manager_lazily(self, store):
        dispatcher = CompanyDispatcher(store=store, recruiter=Recruiter(), org_engine=OrgEngine())
        assert dispatcher.reorg_manager is not None
        assert isinstance(dispatcher.reorg_manager, ReorgManager)

    def test_dispatcher_respects_injected_reorg_manager(self, store):
        org_engine = OrgEngine()
        reorg_manager = ReorgManager(store=store, org_engine=org_engine)
        dispatcher = CompanyDispatcher(
            store=store,
            recruiter=Recruiter(),
            org_engine=org_engine,
            reorg_manager=reorg_manager,
        )
        assert dispatcher.reorg_manager is reorg_manager

    def test_dispatcher_gateway_propose_reorg(self, store):
        dispatcher = CompanyDispatcher(store=store, recruiter=Recruiter(), org_engine=OrgEngine())
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(action="add", replacement_role_id="pm", role={"id": "pm"})
            ]
        )
        proposal = dispatcher.propose_reorg(project_id="p1", summary="reorg", changeset=changeset)
        assert proposal.impact_summary["affected_tasks"] == 0
        assert proposal.impact_summary["affected_employees"] == 0


class TestReorgCommunicationWiring:
    def test_communication_suggester_is_none_by_default(self):
        comms = CommunicationManager(workspace="w", project_id="p", session_id="s")
        assert comms.task_adjustment_suggester is None

    def test_communication_suggester_settable(self, store):
        reorg_manager = ReorgManager(store=store, org_engine=OrgEngine())
        comms = CommunicationManager(workspace="w", project_id="p", session_id="s")
        comms.task_adjustment_suggester = reorg_manager.suggest_task_adjustment
        assert comms.task_adjustment_suggester is not None

    def test_suggester_delegates_to_reorg_manager(self, store):
        reorg_manager = ReorgManager(store=store, org_engine=OrgEngine())
        comms = CommunicationManager(workspace="w", project_id="p", session_id="s")
        comms.task_adjustment_suggester = reorg_manager.suggest_task_adjustment
        result = comms.task_adjustment_suggester(
            project_id="p1",
            source_role_id="engineer",
            summary="low risk adjustment",
            changeset=ReorgChangeSet(task_adjustments=[]),
        )
        assert result["auto_applied"] is False
        assert result["proposal"].scope.value == "task_adjustment"


class TestReorgCustomRuntimeWiring:
    def test_custom_runtime_assembles_reorg(self, store):
        runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))
        bundle = runner.build_org_runtime(roles=[], profile=None)
        assert bundle.reorg_manager is not None
        assert bundle.dispatcher.reorg_manager is bundle.reorg_manager
        assert bundle.communication.task_adjustment_suggester is not None
