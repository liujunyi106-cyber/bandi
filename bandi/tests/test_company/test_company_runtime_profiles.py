from __future__ import annotations

from bandi.company.company_runtime_profiles import (
    CommunicationPolicyConfig,
    CompanyProfile,
    MemoryPolicyConfig,
    ReviewPolicyConfig,
    RoleProfile,
    RuntimeProfileConfig,
    get_builtin_profiles,
    get_builtin_roles,
    get_profile_description,
)


class TestCompanyProfile:
    def test_corporate_profile_has_description(self):
        desc = get_profile_description(CompanyProfile.CORPORATE)
        assert desc
        assert "Hierarchical" in desc

    def test_flat_profile_has_description(self):
        desc = get_profile_description(CompanyProfile.FLAT)
        assert desc
        assert "peer" in desc.lower()

    def test_custom_profile_has_description(self):
        desc = get_profile_description(CompanyProfile.CUSTOM)
        assert desc


class TestBuiltinProfiles:
    def test_contains_corporate(self):
        profiles = get_builtin_profiles()
        assert CompanyProfile.CORPORATE in profiles

    def test_contains_flat(self):
        profiles = get_builtin_profiles()
        assert CompanyProfile.FLAT in profiles

    def test_corporate_has_review_policy(self):
        profiles = get_builtin_profiles()
        cfg = profiles[CompanyProfile.CORPORATE]
        assert cfg.review.require_reviewer_role

    def test_flat_has_no_gates(self):
        profiles = get_builtin_profiles()
        cfg = profiles[CompanyProfile.FLAT]
        assert not cfg.review.enable_gates


class TestBuiltinRoles:
    def test_corporate_has_three_roles(self):
        roles = get_builtin_roles(CompanyProfile.CORPORATE)
        assert len(roles) == 3
        role_ids = {r.id for r in roles}
        assert role_ids == {"manager", "worker", "reviewer"}

    def test_manager_can_spawn(self):
        roles = get_builtin_roles(CompanyProfile.CORPORATE)
        manager = next(r for r in roles if r.id == "manager")
        assert "worker" in manager.can_spawn
        assert "reviewer" in manager.can_spawn

    def test_worker_reports_to_manager(self):
        roles = get_builtin_roles(CompanyProfile.CORPORATE)
        worker = next(r for r in roles if r.id == "worker")
        assert worker.reports_to == "manager"

    def test_flat_has_one_role(self):
        roles = get_builtin_roles(CompanyProfile.FLAT)
        assert len(roles) == 1
        assert roles[0].id == "agent"

    def test_custom_has_no_roles(self):
        roles = get_builtin_roles(CompanyProfile.CUSTOM)
        assert roles == []


class TestRoleProfile:
    def test_default_tools_empty(self):
        role = RoleProfile(id="test", name="Test")
        assert role.tools == []

    def test_default_prompt_refs_empty(self):
        role = RoleProfile(id="test", name="Test")
        assert role.prompt_refs == []

    def test_reports_to_none_by_default(self):
        role = RoleProfile(id="test", name="Test")
        assert role.reports_to is None


class TestPolicyConfigs:
    def test_communication_defaults(self):
        cfg = CommunicationPolicyConfig()
        assert cfg.default_mode == "dm"
        assert not cfg.blocking_default

    def test_memory_defaults(self):
        cfg = MemoryPolicyConfig()
        assert cfg.include_role_memory
        assert cfg.recent_history_lines == 12

    def test_review_defaults(self):
        cfg = ReviewPolicyConfig()
        assert cfg.enable_gates
        assert cfg.allow_human_override

    def test_runtime_profile_defaults(self):
        cfg = RuntimeProfileConfig()
        assert cfg.max_concurrent_work_items == 3
        assert cfg.allow_auto_process
