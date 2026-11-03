from __future__ import annotations

import pytest

from bandi.company.approval import RiskLevel, classify_tool_risk, predict_risk_from_arguments
from bandi.company.comms import (
    append_transcript,
    create_meeting,
    list_inbox,
    mark_read,
    send_message,
)
from bandi.company.communication import CommunicationManager
from bandi.company.company_runtime import CompanyRuntime
from bandi.company.data_acquisition_policy import is_acquisition_shell_command, is_download_command
from bandi.company.gate_harness import GateHarness, GateVerdict
from bandi.company.metadata_ownership import (
    MetadataOwner,
    check_ownership,
    get_owner,
    validate_metadata,
)
from bandi.company.phase import (
    ALLOWED_TRANSITIONS,
    Phase,
    TurnMode,
    classify_turn_for_phase,
    is_terminal,
    kanban_column,
    validate_transition,
    verdict,
)
from bandi.company.phase_hooks import HookContext, HookRegistry
from bandi.company.prompt_contract import build_contract, render_contract, validate_contract
from bandi.company.runtime_identity import CompanyRuntimeIdentity, RuntimeIdentityIndex
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.session_scoping import scoped_member_session_id, scoped_queue_key
from bandi.company.shell_safety import ShellSafetyLevel, classify_shell_command
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item, transition_work_item


class TestPhaseTransitionLifecycle:
    def test_phase_transitions_with_store(self, tmp_path):
        db_path = str(tmp_path / "phases.db")
        store = WorkItemStore(db_path)
        item = create_work_item(
            title="phase lifecycle",
            turn_type=WorkItemTurnType.EXECUTE,
        )
        store.save(item)

        item = transition_work_item(item, Phase.READY, store)
        assert item.phase == Phase.READY

        item = transition_work_item(item, Phase.RUNNING, store)
        assert item.phase == Phase.RUNNING
        assert is_running(item)
        assert kanban_column(item.phase) == "in_progress"

        item = transition_work_item(item, Phase.AWAITING_MANAGER_REVIEW, store)
        assert item.phase == Phase.AWAITING_MANAGER_REVIEW
        assert kanban_column(item.phase) == "in_review"

        item = transition_work_item(item, Phase.APPROVED, store)
        assert is_terminal(item.phase)
        assert verdict(item.phase) == "approve"

        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.APPROVED

    def test_runnable_and_waiting_external(self):
        assert classify_turn_for_phase(Phase.RUNNING) == TurnMode.EXECUTE
        assert classify_turn_for_phase(Phase.READY) == TurnMode.DELEGATE
        assert classify_turn_for_phase(Phase.AWAITING_MANAGER_REVIEW) == TurnMode.REVIEW

        assert is_terminal(Phase.APPROVED)
        assert is_terminal(Phase.FAILED)
        assert is_terminal(Phase.CANCELLED)
        assert not is_terminal(Phase.QUEUED)

        assert verdict(Phase.APPROVED) == "approve"
        assert verdict(Phase.READY_FOR_REWORK) == "rework"
        assert verdict(Phase.FAILED) == "fail"
        assert verdict(Phase.CANCELLED) == "cancel"

    def test_transition_rules(self):
        from bandi.company.phase import InvalidPhaseTransitionError

        for source in Phase:
            for target in Phase:
                if target in ALLOWED_TRANSITIONS.get(source, frozenset()):
                    validate_transition(source, target)
                else:
                    if source == target:
                        continue
                    with pytest.raises(InvalidPhaseTransitionError):
                        validate_transition(source, target)

    def test_hooks_trigger_on_transition(self):
        from datetime import UTC, datetime

        registry = HookRegistry()
        fired: list[str] = []

        def track(ctx: HookContext) -> None:
            fired.append(f"{ctx.source_phase.value}->{ctx.target_phase.value}")

        registry.register("after_transition", track)
        hook_ctx = HookContext(
            work_item_id="wi1",
            source_phase=Phase.QUEUED,
            target_phase=Phase.READY,
            reason="test",
            timestamp=datetime.now(UTC),
        )
        registry.execute("after_transition", hook_ctx)
        assert len(fired) == 1
        assert "queued->ready" in fired[0]

    def test_kanban_column_mapping(self):
        assert kanban_column(Phase.QUEUED) == "todo"
        assert kanban_column(Phase.RUNNING) == "in_progress"
        assert kanban_column(Phase.AWAITING_MANAGER_REVIEW) == "in_review"
        assert kanban_column(Phase.APPROVED) == "done"
        assert kanban_column(Phase.CANCELLED) == "done"


def is_running(item) -> bool:
    return item.phase == Phase.RUNNING


class TestCommunicationCollaborationPipeline:
    def test_message_flow_with_communication_manager(self, tmp_path):
        workspace = str(tmp_path)
        cm = CommunicationManager(workspace, "proj1", "sess1")

        msg_id = cm.send_dm(
            sender_role="manager",
            recipient_role="worker",
            subject="Task assignment",
            body="Please work on task X",
        )
        assert msg_id is not None

        inbox = cm.read_inbox("worker")
        assert len(inbox) == 1
        assert inbox[0].subject == "Task assignment"

        assert cm.mark_as_read("worker", msg_id)
        assert len(cm.read_inbox("worker")) == 0

    def test_broadcast(self, tmp_path):
        workspace = str(tmp_path)
        cm = CommunicationManager(workspace, "proj1", "sess1")

        ids = cm.broadcast(
            sender_role="ceo",
            recipient_roles=["worker_1", "worker_2"],
            subject="All hands",
            body="Company update",
        )
        assert len(ids) == 2

        inbox_1 = cm.read_inbox("worker_1")
        assert len(inbox_1) == 1
        assert inbox_1[0].sender_role == "ceo"

    def test_meeting_and_transcript(self, tmp_path):
        workspace = str(tmp_path)
        meeting_dir = create_meeting(
            workspace, "proj1", "sess1", "m1", "Sprint Planning", ["manager", "worker"]
        )
        assert meeting_dir.exists()

        append_transcript(workspace, "proj1", "sess1", "m1", "manager", "Let's discuss the backlog")
        append_transcript(
            workspace, "proj1", "sess1", "m1", "worker", "I have capacity for 3 tasks"
        )

        content = (meeting_dir / "transcript.md").read_text(encoding="utf-8")
        assert "Let's discuss the backlog" in content
        assert "I have capacity for 3 tasks" in content

    def test_comms_send_list_mark(self, tmp_path):
        workspace = str(tmp_path)
        send_message(workspace, "p1", "s1", "alice", "bob", "hello", "hi there")
        send_message(workspace, "p1", "s1", "alice", "bob", "follow-up", "any updates?")
        inbox = list_inbox(workspace, "p1", "s1", "bob")
        assert len(inbox) == 2

        mark_read(workspace, "p1", "s1", "bob", inbox[0].id)
        assert len(list_inbox(workspace, "p1", "s1", "bob")) == 1
        assert len(list_inbox(workspace, "p1", "s1", "bob", include_seen=True)) == 2


class TestApprovalGateSafetyPipeline:
    def test_approval_gate_shell_flow(self):
        gate = GateHarness(max_reworks=2)

        # 原 `ApprovalEngine.authorize_tool_call` 是**工具面零生产调用**的第二套实现，已删除；
        # 工具审批的唯一生产链路是 `tools/approval.py` + `executor._approval_gate`。
        assert classify_tool_risk("read") == RiskLevel.LOW
        assert classify_tool_risk("bash") == RiskLevel.MEDIUM
        # 参数关键词只会上调风险（注意：这是**关键词**匹配，不认识 shell 语义——
        # `rm -rf /` 由下面的 shell 层拦，不靠风险分级）
        assert predict_risk_from_arguments("bash", {"force": True}) == RiskLevel.CRITICAL

        shell_safe = classify_shell_command("ls -la /tmp")
        assert shell_safe.level == ShellSafetyLevel.SAFE

        shell_denied = classify_shell_command("rm -rf /")
        assert shell_denied.level == ShellSafetyLevel.DENIED

        result = gate.evaluate("wi1", "implement feature X", "implemented feature X with tests")
        assert result.verdict == GateVerdict.PASS

        result_fail = gate.evaluate("wi2", "implement feature Y", "")
        assert result_fail.verdict == GateVerdict.REWORK

        result_blocker = gate.evaluate(
            "wi3", "do Z", "partial", self_reported_risks=["blocker: no API key"]
        )
        assert result_blocker.verdict == GateVerdict.ESCALATE

    def test_gate_stagnation_detection(self):
        gate = GateHarness(max_reworks=1)
        for _ in range(3):
            gate.evaluate("wi_stag", "req", "")
        result = gate.evaluate("wi_stag", "req", "")
        assert result.verdict == GateVerdict.ESCALATE

        gate.reset("wi_stag")
        result = gate.evaluate("wi_stag", "req", "different output")
        assert result.verdict == GateVerdict.PASS

    def test_shell_safety_classification(self):
        assert classify_shell_command("pwd").level == ShellSafetyLevel.SAFE
        assert classify_shell_command("cat file.txt").level == ShellSafetyLevel.SAFE
        assert classify_shell_command("rm -f important.log").level == ShellSafetyLevel.CAUTION
        assert classify_shell_command("mkfs.ext4 /dev/sda1").level == ShellSafetyLevel.DENIED
        assert classify_shell_command("rm -rf /").level == ShellSafetyLevel.DENIED


class TestIdentityScopingOwnershipPipeline:
    def test_identity_and_scoping_flow(self):
        index = RuntimeIdentityIndex()
        identity = CompanyRuntimeIdentity(
            project_id="proj1",
            session_scope_id="scope1",
            role_id="manager",
            employee_id="emp1",
        )
        index.register(identity)

        sid = identity.member_session_id()
        resolved = index.resolve(sid)
        assert resolved is not None
        assert resolved.role_id == "manager"

        by_role = index.resolve_by_role("manager")
        assert len(by_role) == 1
        assert by_role[0].employee_id == "emp1"

        scoped_sid = scoped_member_session_id(
            project_id="proj1",
            session_scope_id="scope1",
            role_id="worker",
            employee_id="emp2",
        )
        assert scoped_sid.startswith("role-session::")
        assert "worker" in scoped_sid

        queue_key = scoped_queue_key(session_scope_id="scope1", role_id="worker")
        assert queue_key == "scope1::worker"

    def test_runtime_with_identity(self):
        rt = CompanyRuntime(
            project_id="proj1", session_scope_id="scope1", role_id="manager", employee_id="emp1"
        )
        assert rt.member_session_id().startswith("role-session::")

        identity = CompanyRuntimeIdentity(
            project_id="proj1", session_scope_id="scope2", role_id="worker", employee_id="emp2"
        )
        rt.register_identity(identity)
        resolved = rt.resolve_identity(identity.member_session_id())
        assert resolved is not None
        assert resolved.role_id == "worker"

        item = create_work_item(title="runtime task", turn_type=WorkItemTurnType.EXECUTE)
        rt.push_to_queue(item)
        assert rt.queue_size() == 1
        assert rt.find_work_item(item.id) is not None

        popped = rt.pop_from_queue()
        assert popped is not None
        assert popped.id == item.id

        rt.set_metadata("key1", "value1")
        assert rt.metadata("key1") == "value1"
        assert rt.metadata("missing", "default") == "default"

    def test_metadata_ownership_validation(self):
        owner = get_owner("title")
        assert owner == MetadataOwner.WORK_ITEM
        owner = get_owner("session_id")
        assert owner == MetadataOwner.RUNTIME_TASK

        issue = check_ownership("title", "runtime_task")
        assert issue is not None
        assert issue.code == "ownership_violation"
        issue = check_ownership("title", "work_item")
        assert issue is None

        metadata = {"title": "test", "session_id": "sess-1", "execution_log": "done"}
        issues = validate_metadata(metadata, "work_item")
        assert len(issues) == 2
        assert all(i.code == "ownership_violation" for i in issues)

    def test_index_remove(self):
        index = RuntimeIdentityIndex()
        id1 = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e"
        )
        index.register(id1)
        assert index.resolve(id1.member_session_id()) is not None
        index.remove(id1.member_session_id())
        assert index.resolve(id1.member_session_id()) is None


class TestContractExternalAcquisitionPipeline:
    def test_prompt_contract_lifecycle(self):
        contract = build_contract(
            work_item_id="wi1",
            title="Implement login",
            turn_type="execute",
            phase="ready",
            assignee_role="worker",
            description="OAuth login page",
            parent_context="sprint-12",
            dependencies=["wi0"],
            output_format="code + tests",
        )
        assert contract["work_item_id"] == "wi1"
        assert contract["title"] == "Implement login"

        rendered = render_contract(contract)
        assert "Implement login" in rendered
        assert "OAuth login page" in rendered
        assert "execute" in rendered

        errors = validate_contract(contract)
        assert len(errors) == 0

        minimal = build_contract(
            work_item_id="wi2",
            title="Quick fix",
            turn_type="execute",
            phase="ready",
        )
        assert validate_contract(minimal) == []

    def test_acquisition_policy_classification(self):
        assert is_acquisition_shell_command("curl http://example.com")
        assert is_acquisition_shell_command("wget http://example.com/file.zip")
        assert is_download_command("curl -o file.zip http://example.com/file.zip")
        assert not is_acquisition_shell_command("ls -la")


class TestSeatExecutorPipeline:
    @pytest.mark.asyncio
    async def test_seat_executor_lifecycle(self):
        executor = EngineSeatExecutor()
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="worker", employee_id="e")
        item = create_work_item(title="exec task", turn_type=WorkItemTurnType.EXECUTE)

        await executor.prepare_seat(rt, item)
        result = await executor.run_turn(rt, item)
        assert isinstance(result, SeatResult)

        checkpoint = await executor.checkpoint(rt)
        assert isinstance(checkpoint, dict)

        await executor.interrupt(rt)
        await executor.shutdown()
