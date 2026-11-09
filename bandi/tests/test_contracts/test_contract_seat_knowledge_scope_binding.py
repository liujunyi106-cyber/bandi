# 功能：契约测试——员工档案绑定的知识库成为检索范围（角色权限缺省时回落）
from __future__ import annotations

from typing import Any

from bandi.company.seat_executor import EngineSeatExecutor


class _Role:
    permissions: dict[str, Any] = {}


class _Engine:
    def get_role(self, role_id: str) -> _Role:
        return _Role()


class _Resource:
    resource_type = "knowledge_base"
    resource_id = "kb-requirements"
    status = "active"


class _Profile:
    resources = [_Resource()]


class _ProfileManager:
    def get_profile(self, employee_id: str) -> _Profile:
        return _Profile()


class TestSeatKnowledgeScope:
    def test_bound_kb_used_when_role_permission_missing(self, monkeypatch) -> None:
        import bandi.company.agent_profile as agent_profile

        monkeypatch.setattr(agent_profile, "AgentProfileManager", _ProfileManager)
        executor = EngineSeatExecutor.__new__(EngineSeatExecutor)
        executor._org_engine = _Engine()
        assert executor._role_knowledge_bases("aid_requirements", "aid_requirements") == [
            "kb-requirements"
        ]

    def test_role_permission_takes_precedence(self, monkeypatch) -> None:
        class _PermRole:
            permissions = {"knowledge_base": ["kb-explicit"]}

        class _PermEngine:
            def get_role(self, role_id: str) -> _PermRole:
                return _PermRole()

        executor = EngineSeatExecutor.__new__(EngineSeatExecutor)
        executor._org_engine = _PermEngine()
        assert executor._role_knowledge_bases("r", "emp") == ["kb-explicit"]

    def test_no_binding_no_filter(self, monkeypatch) -> None:
        class _EmptyProfile:
            resources: list = []

        class _EmptyManager:
            def get_profile(self, employee_id: str) -> _EmptyProfile:
                return _EmptyProfile()

        import bandi.company.agent_profile as agent_profile

        monkeypatch.setattr(agent_profile, "AgentProfileManager", _EmptyManager)
        executor = EngineSeatExecutor.__new__(EngineSeatExecutor)
        executor._org_engine = _Engine()
        assert executor._role_knowledge_bases("r", "emp") is None
