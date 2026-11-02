from __future__ import annotations

import pytest

from bandi.company.company_runtime import (
    CompanyMemberSession,
    CompanyRuntime,
    CompanyRuntimeState,
    RoleRuntimeSession,
    canonical_role_session_id,
    normalize_role_runtime_status,
    parse_role_session_id,
)
from bandi.company.phase import Phase
from bandi.company.runtime_identity import CompanyRuntimeIdentity
from bandi.company.session_scoping import scoped_queue_key
from bandi.company.work_item import WorkItemTurnType, create_work_item


class TestCompanyRuntime:
    def test_member_session_id(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        sid = rt.member_session_id()
        assert sid.startswith("role-session::")

    def test_register_and_resolve_identity(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s2", role_id="worker", employee_id="e2"
        )
        rt.register_identity(identity)
        resolved = rt.resolve_identity(identity.member_session_id())
        assert resolved is not None
        assert resolved.role_id == "worker"

    def test_push_and_pop_queue(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        item = create_work_item(title="task1", turn_type=WorkItemTurnType.EXECUTE)
        rt.push_to_queue(item)
        assert rt.queue_size() == 1
        popped = rt.pop_from_queue()
        assert popped is not None
        assert popped.id == item.id
        assert rt.queue_size() == 0

    def test_pop_empty_queue(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        assert rt.pop_from_queue() is None

    def test_list_queue(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        items = [
            create_work_item(title=f"task{i}", turn_type=WorkItemTurnType.EXECUTE) for i in range(3)
        ]
        for item in items:
            rt.push_to_queue(item)
        assert len(rt.list_queue()) == 3

    def test_metadata(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        rt.set_metadata("key1", "value1")
        assert rt.metadata("key1") == "value1"
        assert rt.metadata("nonexistent", "default") == "default"

    def test_count_by_phase(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        for _ in range(2):
            rt.push_to_queue(create_work_item(title="task", turn_type=WorkItemTurnType.EXECUTE))
        counts = rt.count_work_items_by_phase()
        assert counts.get("queued", 0) == 2

    def test_count_by_phase_empty_queue(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        assert rt.count_work_items_by_phase() == {}

    def test_count_by_phase_mixed(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        rt.push_to_queue(create_work_item(title="a", turn_type=WorkItemTurnType.EXECUTE))
        rt.push_to_queue(create_work_item(title="b", turn_type=WorkItemTurnType.EXECUTE))
        rt.push_to_queue(
            create_work_item(title="c", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.READY)
        )
        counts = rt.count_work_items_by_phase()
        assert counts["queued"] == 2
        assert counts["ready"] == 1

    def test_find_work_item(self):
        rt = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        item = create_work_item(title="find me", turn_type=WorkItemTurnType.EXECUTE)
        rt.push_to_queue(item)
        found = rt.find_work_item(item.id)
        assert found is not None
        assert found.title == "find me"


def _runtime(**kwargs) -> CompanyRuntime:
    defaults = dict(project_id="p", session_scope_id="s", role_id="worker", employee_id="e")
    defaults.update(kwargs)
    return CompanyRuntime(**defaults)


class TestCompanyRuntimeState:
    def test_default_state_empty(self):
        state = CompanyRuntimeState()
        assert state.member_sessions == {}
        assert state.role_sessions == {}
        assert state.queued_work_item_ids == set()
        assert state.claimed_work_item_ids == set()
        assert state.home_team_instance_by_role == {}

    def test_role_queues_is_defaultdict_of_deque(self):
        state = CompanyRuntimeState()
        assert len(state.role_queues["a"]) == 0
        assert len(state.role_queues["b"]) == 0

    def test_runtime_has_state(self):
        rt = _runtime()
        assert isinstance(rt.state, CompanyRuntimeState)


class TestCanonicalRoleSessionId:
    def test_canonical_format(self):
        assert (
            canonical_role_session_id(run_id="run1", role_id="worker")
            == "role-runtime::run1::worker"
        )

    def test_strips_whitespace(self):
        assert (
            canonical_role_session_id(run_id="  run1  ", role_id="  worker  ")
            == "role-runtime::run1::worker"
        )

    def test_missing_run_id_raises(self):
        with pytest.raises(ValueError):
            canonical_role_session_id(run_id="", role_id="worker")

    def test_missing_role_id_raises(self):
        with pytest.raises(ValueError):
            canonical_role_session_id(run_id="run1", role_id="")

    def test_team_instance_id_embeds_team(self):
        with_team = canonical_role_session_id(
            run_id="run1", role_id="worker", team_instance_id="t1"
        )
        assert with_team == "role-runtime::run1::t1::worker"
        assert with_team != canonical_role_session_id(run_id="run1", role_id="worker")


class TestParseRoleSessionId:
    def test_parse_canonical(self):
        assert parse_role_session_id("role-runtime::run1::worker") == ("run1", "worker")

    def test_parse_empty(self):
        assert parse_role_session_id("") is None
        assert parse_role_session_id(None) is None

    def test_parse_non_canonical_prefix(self):
        assert parse_role_session_id("role-session::a::b") is None

    def test_parse_team_scoped_segments(self):
        assert parse_role_session_id("role-runtime::a::b::c") == ("a", "b::c")

    def test_parse_missing_role(self):
        assert parse_role_session_id("role-runtime::run1::") is None

    def test_roundtrip(self):
        rid = canonical_role_session_id(run_id="run1", role_id="worker")
        assert parse_role_session_id(rid) == ("run1", "worker")


class TestSessionModels:
    def test_member_session_defaults(self):
        s = CompanyMemberSession()
        assert s.status == "idle"
        assert s.member_session_id
        assert s.focused_work_item_id == ""

    def test_role_session_defaults(self):
        s = RoleRuntimeSession(run_id="r1", role_id="worker")
        assert s.status == "idle"
        assert s.role_session_id
        assert s.focused_work_item_id == ""


class TestEnsureSessions:
    def test_ensure_member_session_creates_once(self):
        rt = _runtime()
        s1 = rt.ensure_member_session()
        s2 = rt.ensure_member_session()
        assert s1 is s2
        assert s1.member_session_id == rt.member_session_id()
        assert rt.state.member_sessions[s1.member_session_id] is s1
        assert s1.role_id == "worker"
        assert s1.role_session_id == "role-runtime::p::worker"

    def test_ensure_role_session_uses_canonical_key(self):
        rt = _runtime()
        rs = rt.ensure_role_session(run_id="run1")
        assert rt.state.role_sessions["role-runtime::run1::worker"] is rs
        assert rs.run_id == "run1"
        assert rs.role_id == "worker"

    def test_ensure_role_session_default_run(self):
        rt = _runtime()
        rs = rt.ensure_role_session()
        assert rt.state.role_sessions["role-runtime::p::worker"] is rs

    def test_session_for_work_item_binds(self):
        rt = _runtime()
        item = create_work_item(title="task", turn_type=WorkItemTurnType.EXECUTE)
        session = rt.session_for_work_item(item)
        assert session.current_task_id == item.id
        assert session.focused_work_item_id == item.id


class TestEnqueueRunnableWorkItems:
    def test_enqueue_routes_by_role_scope(self):
        rt = _runtime()
        items = [
            create_work_item(
                title=f"t{i}", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
            )
            for i in range(3)
        ]
        rt.enqueue_runnable_work_items(items)
        key = scoped_queue_key(session_scope_id="s", role_id="worker")
        queue = rt.state.role_queues[key]
        assert len(queue) == 3
        assert all(str(tag).startswith("work-item::") for tag in queue)

    def test_enqueue_review_priority(self):
        rt = _runtime()
        execute = create_work_item(
            title="exec", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        review = create_work_item(
            title="rev", turn_type=WorkItemTurnType.REVIEW, assignee_role="worker"
        )
        rt.enqueue_runnable_work_items([execute, review])
        key = scoped_queue_key(session_scope_id="s", role_id="worker")
        queue = rt.state.role_queues[key]
        assert queue[0] == f"review-work-item::{review.id}"
        assert queue[1] == f"work-item::{execute.id}"

    def test_enqueue_dedup(self):
        rt = _runtime()
        item = create_work_item(
            title="dup", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        rt.enqueue_runnable_work_items([item])
        rt.enqueue_runnable_work_items([item])
        key = scoped_queue_key(session_scope_id="s", role_id="worker")
        assert len(rt.state.role_queues[key]) == 1

    def test_enqueue_skips_terminal_phase(self):
        rt = _runtime()
        item = create_work_item(
            title="done",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.APPROVED,
        )
        rt.enqueue_runnable_work_items([item])
        assert rt.state.role_queues == {}

    def test_enqueue_skips_in_progress_phase(self):
        rt = _runtime()
        item = create_work_item(
            title="running",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.RUNNING,
        )
        rt.enqueue_runnable_work_items([item])
        assert rt.state.role_queues == {}

    def test_enqueue_skips_waiting_dependencies(self):
        rt = _runtime()
        item = create_work_item(
            title="waiting-deps",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.WAITING_DEPENDENCIES,
        )
        rt.enqueue_runnable_work_items([item])
        assert rt.state.role_queues == {}

    def test_enqueue_skips_missing_role(self):
        rt = _runtime()
        item = create_work_item(title="norole", turn_type=WorkItemTurnType.EXECUTE)
        rt.enqueue_runnable_work_items([item])
        assert rt.state.role_queues == {}

    def test_enqueue_tracks_queued_ids(self):
        rt = _runtime()
        item = create_work_item(
            title="track", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        rt.enqueue_runnable_work_items([item])
        assert item.id in rt.state.queued_work_item_ids

    def test_enqueue_dedup_via_queued_index_not_deque_scan(self):
        rt = _runtime()
        item = create_work_item(
            title="idx-dedup", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        rt.state.queued_work_item_ids.add(item.id)
        rt.enqueue_runnable_work_items([item])
        key = scoped_queue_key(session_scope_id="s", role_id="worker")
        assert len(rt.state.role_queues[key]) == 0


class TestPopNextWorkItemId:
    def test_pop_returns_id_and_clears_queued(self):
        rt = _runtime()
        item = create_work_item(
            title="popid", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        rt.enqueue_runnable_work_items([item])
        work_item_id = rt.pop_next_work_item_id("worker")
        assert work_item_id == item.id
        assert item.id not in rt.state.queued_work_item_ids

    def test_pop_empty_returns_none(self):
        rt = _runtime()
        assert rt.pop_next_work_item_id("worker") is None

    def test_pop_review_first(self):
        rt = _runtime()
        execute = create_work_item(
            title="exec", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        review = create_work_item(
            title="review", turn_type=WorkItemTurnType.REVIEW, assignee_role="worker"
        )
        rt.enqueue_runnable_work_items([execute, review])
        assert rt.pop_next_work_item_id("worker") == review.id


class TestStateManagement:
    def test_default_state_fresh(self):
        rt = _runtime()
        assert rt.state is not None
        assert rt.state.member_sessions == {}
        assert rt.state.claimed_work_item_ids == set()

    def test_use_state_isolates_scheduler_visibility(self):
        rt = _runtime()
        scheduler_state = rt.create_state()
        token = rt.use_state(scheduler_state)
        rt.ensure_role_member_session("worker")
        assert "worker" in str(list(scheduler_state.member_sessions))
        rt.reset_state(token)
        assert rt.state.member_sessions == {}

    def test_reset_state_restores_default(self):
        rt = _runtime()
        token = rt.use_state(rt.create_state())
        rt.ensure_member_session()
        assert rt.state.member_sessions
        rt.reset_state(token)
        assert rt.state.member_sessions == {}

    def test_ensure_role_member_session_scoped(self):
        rt = _runtime()
        session = rt.ensure_role_member_session("worker")
        assert session.role_id == "worker"
        assert rt.ensure_role_member_session("worker") is session


class TestClaimReleaseWorkItem:
    def test_claim_marks_running_and_registers(self):
        rt = _runtime()
        assert rt.claim_work_item("wi-1", role_id="worker")
        assert "wi-1" in rt.state.claimed_work_item_ids
        session = rt.state.member_sessions[rt._session_id_for_role("worker")]
        assert session.status == "running"
        assert session.focused_work_item_id == "wi-1"

    def test_claim_empty_false(self):
        rt = _runtime()
        assert not rt.claim_work_item("", role_id="worker")

    def test_release_restores_idle(self):
        rt = _runtime()
        rt.claim_work_item("wi-1", role_id="worker")
        assert rt.release_work_item("wi-1", role_id="worker")
        assert rt.state.claimed_work_item_ids == set()
        session = rt.state.member_sessions[rt._session_id_for_role("worker")]
        assert session.status == "idle"
        assert session.focused_work_item_id == ""

    def test_release_unclaimed_is_safe(self):
        rt = _runtime()
        assert not rt.release_work_item("missing", role_id="worker")
        assert rt.state.claimed_work_item_ids == set()

    def test_release_does_not_touch_other_role_session(self):
        rt = _runtime()
        rt.claim_work_item("wi-1", role_id="worker")
        rt.claim_work_item("wi-2", role_id="manager")
        rt.release_work_item("wi-1", role_id="worker")
        manager_session = rt.state.member_sessions[rt._session_id_for_role("manager")]
        assert manager_session.status == "running"


class TestReleaseAndSessionKey:
    def test_release_empty_false(self):
        rt = _runtime()
        assert not rt.release_work_item("")

    def test_release_idempotent_double_release(self):
        rt = _runtime()
        rt.claim_work_item("wi-1", role_id="worker")
        assert rt.release_work_item("wi-1", role_id="worker")
        assert not rt.release_work_item("wi-1", role_id="worker")

    def test_session_id_for_role_empty_matches_member_session(self):
        rt = _runtime(role_id="worker", employee_id="")
        assert rt._session_id_for_role("") == rt.member_session_id()

    def test_session_id_for_role_explicit_role(self):
        rt = _runtime(employee_id="")
        assert (
            rt._session_id_for_role("worker")
            == rt.ensure_role_member_session("worker").member_session_id
        )

    def test_claim_release_roundtrip_no_role_id(self):
        rt = _runtime(role_id="worker", employee_id="")
        item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
        assert rt.claim_work_item(item.id)
        assert rt.release_work_item(item.id)
        assert rt.state.claimed_work_item_ids == set()
        session = rt.state.member_sessions[rt.member_session_id()]
        assert session.status == "idle"

    def test_state_returns_default_when_context_none(self):
        rt = _runtime()
        assert rt.state is rt._default_state

    def test_public_dispatch_api(self):
        rt = _runtime()
        item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
        assert rt.is_dispatchable(item)
        session = rt.ensure_member_session()
        assert (
            rt.set_member_session_status(session, "running", focused_work_item_id=item.id)
            == "running"
        )
        assert rt.sorted_member_sessions() == [session]


class TestNormalizeRoleRuntimeStatus:
    def test_folds_idle(self):
        assert normalize_role_runtime_status("idle", "wi-1") == "idle"

    def test_running_without_focus_is_idle(self):
        assert normalize_role_runtime_status("running", "") == "idle"

    def test_running_with_focus_stays_running(self):
        assert normalize_role_runtime_status("running", "wi-1") == "running"

    def test_blocked_keeps_blocked(self):
        assert normalize_role_runtime_status("blocked", "") == "blocked"

    def test_cold_maps_to_idle(self):
        assert normalize_role_runtime_status("cold", "wi-1") == "idle"

    def test_unknown_falls_back_to_default(self):
        assert normalize_role_runtime_status("weird", "", default="idle") == "idle"


class TestSessionSortKey:
    def test_idle_sorts_before_running(self):
        rt = _runtime()
        idle = rt.ensure_role_member_session("worker")
        rt._set_member_session_status(idle, "idle")
        running = rt.ensure_role_member_session("manager")
        rt._set_member_session_status(running, "running", focused_work_item_id="wi")
        assert rt._session_sort_key(idle) < rt._session_sort_key(running)
