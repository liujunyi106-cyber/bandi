from __future__ import annotations

import pytest

from bandi.company.org_engine import OrgEmployee, OrgEngine, OrgMember


class TestOrgEngine:
    def test_register_and_get(self):
        engine = OrgEngine()
        engine.register_role("manager", "Manager", "Oversees work")
        agent = engine.get_agent("manager")
        assert agent is not None
        assert agent.name == "Manager"

    def test_register_role_unknown_reports_to_rejected(self):
        with pytest.raises(ValueError):
            OrgEngine().register_role("x", "X", reports_to="ghost")

    def test_register_role_self_reports_to_rejected(self):
        with pytest.raises(ValueError):
            OrgEngine().register_role("x", "X", reports_to="x")

    def test_register_role_reports_to_cycle_rejected(self):
        engine = OrgEngine()
        engine.register_role("b", "B")
        engine.register_role("a", "A", reports_to="b")
        engine._roles["b"].reports_to = "a"
        with pytest.raises(ValueError):
            engine.register_role("c", "C", reports_to="a")

    def test_apply_changeset_cycle_rejected(self):
        from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

        engine = OrgEngine()
        engine.register_role("b", "B")
        engine.register_role("a", "A", reports_to="b")

        # 同一批：add c（reports_to=a）+ replace a（reports_to=c）→ 环 a<->c
        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="add",
                    role_id="c",
                    role={"id": "c", "name": "C", "reports_to": "a"},
                ),
                ReorgRoleChange(
                    action="replace",
                    role_id="a",
                    replacement_role_id="a",
                    role={"id": "a", "name": "A", "reports_to": "c"},
                ),
            ]
        )
        with pytest.raises(ValueError, match="cycle"):
            engine.apply_changeset(changeset)
        assert engine.get_role("c") is None, "环检测失败时不得写入部分状态"
        assert engine.get_role("a").reports_to == "b", "环检测失败时原角色不得被修改"

        # replace a 的 reports_to 指向其下级 c → 环
        engine.register_role("c", "C", reports_to="a")
        changeset2 = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="replace",
                    role_id="a",
                    replacement_role_id="a",
                    role={"id": "a", "name": "A", "reports_to": "c"},
                ),
            ]
        )
        with pytest.raises(ValueError, match="cycle"):
            engine.apply_changeset(changeset2)

    def test_apply_changeset_update_preserves_role_id(self):
        """修复：update 分支 merge 后必须保留角色 id——_role_to_dict 输出
        role_id 键而 _role_from_dict 读 id 键，缺失回填会导致 id 清空为 ""。"""
        from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

        engine = OrgEngine()
        engine.register_role("dev", "Dev", "writes code")

        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="update",
                    role_id="dev",
                    role={"responsibility": "new responsibility"},
                ),
            ]
        )
        engine.apply_changeset(changeset)

        role = engine.get_role("dev")
        assert role is not None
        assert role.id == "dev", f"update 后角色 id 被清空: {role.id!r}"
        assert role.responsibility == "new responsibility"
        assert engine.get_agent("dev") is not None, "_roles 字典键与 role.id 必须一致"

    def test_apply_changeset_update_ignores_payload_id_override(self):
        """同族：update 是原位更新——payload 携带不同 id 不得造成
        字典键与 role.id 不一致（改 id 应走 replace）。"""
        from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

        engine = OrgEngine()
        engine.register_role("dev", "Dev")

        changeset = ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="update",
                    role_id="dev",
                    role={"id": "hijacked", "name": "Dev2"},
                ),
            ]
        )
        engine.apply_changeset(changeset)

        role = engine.get_role("dev")
        assert role is not None
        assert role.id == "dev", "update 不得通过 payload.id 改写角色身份"
        assert role.name == "Dev2", "除 id 外的字段更新仍应生效"

    def test_role_serialize_roundtrip_preserves_id(self):
        """同族：_role_to_dict/_role_from_dict 序列化对必须对称——
        双键兼容（id/role_id）防止未来直连调用再踩键名错位。"""
        engine = OrgEngine()
        engine.register_role("dev", "Dev", "resp", can_spawn=["qa"])

        payload = engine._role_to_dict(engine.get_role("dev"))
        restored = engine._role_from_dict(payload)

        assert restored.id == "dev"
        assert restored.name == "Dev"
        assert restored.responsibility == "resp"
        assert list(restored.can_spawn) == ["qa"]

    def test_get_chain_of_command_cycle_detected(self):
        engine = OrgEngine()
        engine.register_role("a", "A")
        engine.register_role("b", "B", reports_to="a")
        engine._roles["a"].reports_to = "b"
        with pytest.raises(ValueError):
            engine.get_chain_of_command("a")

    def test_register_with_reports_to(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO", "Top level")
        engine.register_role("worker", "Worker", "Does work", reports_to="ceo")
        assert engine.get_agent("worker").reports_to == "ceo"

    def test_list_agents(self):
        engine = OrgEngine()
        engine.register_role("a", "A")
        engine.register_role("b", "B")
        assert len(engine.list_agents()) >= 2
        assert "a" in [r.role_id for r in engine.list_agents()]

    def test_get_top_level_role_ids(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO")
        engine.register_role("worker", "Worker", reports_to="ceo")
        tops = engine.get_top_level_role_ids()
        assert "ceo" in tops

    def test_get_final_decider(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO")
        engine.register_role("worker", "Worker", reports_to="ceo")
        assert engine.get_final_decider_role_id() == "ceo"

    def test_get_executor(self):
        engine = OrgEngine()
        engine.register_role("worker", "Worker")
        assert engine.get_executor("worker") is not None
        assert engine.get_executor("nonexistent") is None

    def test_get_coordinator(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO")
        coord = engine.get_coordinator()
        assert coord is not None
        assert coord.role_id == "ceo"

    def test_get_allowed_contact_roles(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO")
        engine.register_role("manager", "Manager", reports_to="ceo")
        engine.register_role("worker", "Worker", reports_to="manager")
        contacts = engine.get_allowed_contact_roles("worker")
        assert "manager" in contacts

    def test_get_role_prompt_context(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO", "Runs company")
        ctx = engine.get_role_prompt_context("ceo")
        assert "CEO（ceo）" in ctx
        assert "Runs company" in ctx
        assert engine.get_role_prompt_context("ghost") == ""

    def test_get_chain_of_command(self):
        engine = OrgEngine()
        engine.register_role("ceo", "CEO")
        engine.register_role("manager", "Manager", reports_to="ceo")
        engine.register_role("worker", "Worker", reports_to="manager")
        chain = engine.get_chain_of_command("worker")
        assert chain == ["worker", "manager", "ceo"]

    def test_can_spawn(self):
        engine = OrgEngine()
        engine.register_role("manager", "Manager", can_spawn=["worker"])
        engine.register_role("worker", "Worker")
        assert engine.can_spawn("manager", "worker") is True
        assert engine.can_spawn("worker", "manager") is False

    def test_builtin_roles_loaded(self):
        engine = OrgEngine()
        assert engine.get_role("ceo") is not None
        assert engine.get_role("cto") is not None
        assert engine.get_role("engineer") is not None

    def test_register_member(self):
        engine = OrgEngine()
        member = OrgMember(user_id="u1", role_id="engineer", name="Alice")
        engine.register_member(member)
        assert engine.get_member("u1") is member
        assert len(engine.list_members()) == 1

    def test_role_id_property(self):
        engine = OrgEngine()
        role = engine.register_role("test_role", "Test")
        assert role.role_id == "test_role"
        assert role.id == "test_role"


class TestOrgEmployee:
    def _engine(self) -> OrgEngine:
        engine = OrgEngine()
        engine.register_employee(
            OrgEmployee(
                employee_id="e1",
                name="Alice",
                role_id="engineer",
                category="engineering",
                domains=["python"],
                metadata={"template_id": "general_engineer"},
            )
        )
        engine.register_employee(
            OrgEmployee(
                employee_id="e2",
                name="Bob",
                role_id="analyst",
                category="analytics",
                domains=["sql"],
            )
        )
        return engine

    def test_register_and_get(self):
        engine = self._engine()
        employee = engine.get_employee("e1")
        assert employee is not None
        assert employee.name == "Alice"
        assert employee.role_id == "engineer"

    def test_get_missing_returns_none(self):
        assert OrgEngine().get_employee("nope") is None

    def test_list_all_employees(self):
        engine = self._engine()
        assert len(engine.list_employees()) == 2

    def test_list_employees_filter_by_role(self):
        engine = self._engine()
        engineers = engine.list_employees(role_id="engineer")
        assert [e.employee_id for e in engineers] == ["e1"]

    def test_list_employees_empty(self):
        assert OrgEngine().list_employees() == []

    def test_employee_role_ids_primary_and_metadata(self):
        engine = OrgEngine()
        engine.register_employee(
            OrgEmployee(
                employee_id="e1",
                name="Alice",
                role_id="engineer",
                metadata={"role_ids": ["analyst"]},
            )
        )
        ids = engine.employee_role_ids(engine.get_employee("e1"))
        assert ids[0] == "engineer"
        assert "analyst" in ids
