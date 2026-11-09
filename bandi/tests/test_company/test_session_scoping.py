from __future__ import annotations

from bandi.company.session_scoping import (
    scoped_member_session_id,
    scoped_queue_key,
    task_session_scope_id,
)


class TestSessionScoping:
    def test_scoped_member_session_id_basic(self):
        result = scoped_member_session_id(
            project_id="proj1", session_scope_id="scope1", role_id="manager", employee_id="emp1"
        )
        assert result.startswith("role-session::")
        assert "proj1" in result
        assert "manager" in result

    def test_scoped_member_session_id_explicit(self):
        result = scoped_member_session_id(
            project_id="p",
            session_scope_id="s",
            role_id="r",
            employee_id="e",
            explicit_id="custom-id",
        )
        assert result == "custom-id"

    def test_scoped_member_session_id_defaults(self):
        result = scoped_member_session_id(
            project_id="", session_scope_id="", role_id="", employee_id=""
        )
        assert "default" in result
        assert "unknown" in result

    def test_scoped_member_session_id_with_team(self):
        result = scoped_member_session_id(
            project_id="p",
            session_scope_id="s",
            role_id="r",
            employee_id="e",
            team_instance_id="team1",
        )
        assert "team1" in result

    def test_scoped_queue_key_basic(self):
        result = scoped_queue_key(session_scope_id="scope1", role_id="worker")
        assert result == "scope1::worker"

    def test_scoped_queue_key_no_role(self):
        assert scoped_queue_key(session_scope_id="s", role_id="") == ""

    def test_scoped_queue_key_with_team(self):
        result = scoped_queue_key(session_scope_id="s", role_id="r", team_instance_id="t")
        assert result == "s::t::r"

    def test_task_session_scope_id_parent(self):
        result = task_session_scope_id({"parent_session_id": "parent-1", "session_id": "sess-1"})
        assert result == "parent-1"

    def test_task_session_scope_id_fallback(self):
        result = task_session_scope_id({"session_id": "sess-1"})
        assert result == "sess-1"

    def test_task_session_scope_id_empty(self):
        assert task_session_scope_id(None) == ""
        assert task_session_scope_id({}) == ""
