from __future__ import annotations

from bandi.company.runtime_identity import CompanyRuntimeIdentity, RuntimeIdentityIndex


class TestCompanyRuntimeIdentity:
    def test_member_session_id(self):
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e"
        )
        sid = identity.member_session_id()
        assert sid.startswith("role-session::")
        assert "p" in sid
        assert "r" in sid

    def test_scope_key(self):
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e"
        )
        assert identity.scope_key() == "s"

    def test_scope_key_no_scope(self):
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="", role_id="r", employee_id="e"
        )
        assert identity.scope_key() == "p"

    def test_str(self):
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e", explicit_id="custom"
        )
        assert str(identity) == "custom"


class TestRuntimeIdentityIndex:
    def test_register_and_resolve(self):
        index = RuntimeIdentityIndex()
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e"
        )
        index.register(identity)
        resolved = index.resolve(identity.member_session_id())
        assert resolved is not None
        assert resolved.role_id == "r"

    def test_resolve_nonexistent(self):
        index = RuntimeIdentityIndex()
        assert index.resolve("nonexistent") is None

    def test_resolve_by_role(self):
        index = RuntimeIdentityIndex()
        i1 = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s1", role_id="worker", employee_id="e1"
        )
        i2 = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s2", role_id="worker", employee_id="e2"
        )
        i3 = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s3", role_id="manager", employee_id="e3"
        )
        index.register(i1)
        index.register(i2)
        index.register(i3)
        workers = index.resolve_by_role("worker")
        assert len(workers) == 2

    def test_remove(self):
        index = RuntimeIdentityIndex()
        identity = CompanyRuntimeIdentity(
            project_id="p", session_scope_id="s", role_id="r", employee_id="e"
        )
        index.register(identity)
        index.remove(identity.member_session_id())
        assert index.resolve(identity.member_session_id()) is None
