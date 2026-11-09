from __future__ import annotations

import pytest

from bandi.core.permission.builtin.entity_policy import EntityPolicy, EntityScopeCondition
from bandi.core.permission.models import Decision, PermissionContext, PermissionDecision, Resource
from bandi.core.permission.policy import (
    AttributeCondition,
    ConditionPolicy,
    SimpleEffect,
)
from bandi.core.permission.policy_engine import PolicyEngine
from bandi.core.permission.roles import (
    ADMIN,
    MEMBER,
    POWER_USER,
    READONLY,
    check_role,
    get_builtin_role,
    list_builtin_roles,
)


class TestModels:
    def test_resource_defaults(self):
        r = Resource(type="tool", name="bash", action="execute")
        assert r.attributes == {}

    def test_decision_values(self):
        assert Decision.ALLOW.value == "allow"
        assert Decision.ASK.value == "ask"
        assert Decision.DENY.value == "deny"

    def test_permission_decision_default_scope(self):
        d = PermissionDecision(decision=Decision.ALLOW)
        assert d.scope.value == "once"


class TestPolicyEngine:
    def test_empty_engine_allows(self):
        engine = PolicyEngine()
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert engine.check(r, ctx).decision == Decision.ALLOW

    def test_deny_short_circuits(self):
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"action": "write"}),
                SimpleEffect(Decision.DENY, "no write"),
            )
        )
        engine.add(
            ConditionPolicy(
                AttributeCondition({}),
                SimpleEffect(Decision.ALLOW, "allow all"),
            )
        )
        r = Resource(type="tool", name="write", action="write")
        ctx = PermissionContext()
        result = engine.check(r, ctx)
        assert result.decision == Decision.DENY
        assert "no write" in result.reason

    def test_ask_if_no_deny(self):
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"action": "execute"}),
                SimpleEffect(Decision.ASK, "needs approval"),
            )
        )
        r = Resource(type="tool", name="bash", action="execute")
        ctx = PermissionContext()
        result = engine.check(r, ctx)
        assert result.decision == Decision.ASK

    def test_cache_hit(self):
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"action": "read"}),
                SimpleEffect(Decision.ALLOW, "ok"),
            )
        )
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert engine.check(r, ctx).decision == Decision.ALLOW
        assert engine.check(r, ctx).decision == Decision.ALLOW

    def test_cache_hit_with_tool_obj_attribute(self):
        """attributes 含 tool_obj（对象）时指纹必须稳定——排除对象键
        否则 default=str 含内存地址每次不同 → 缓存恒 miss。"""
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"action": "execute"}),
                SimpleEffect(Decision.DENY, "no exec"),
            )
        )
        obj_a = object()
        obj_b = object()
        r1 = Resource(
            type="tool",
            name="bash",
            action="execute",
            attributes={"arguments": {"a": 1}, "tool_obj": obj_a},
        )
        r2 = Resource(
            type="tool",
            name="bash",
            action="execute",
            attributes={"arguments": {"a": 1}, "tool_obj": obj_b},
        )
        ctx = PermissionContext()
        assert engine.check(r1, ctx).decision == Decision.DENY
        assert engine.check(r2, ctx).decision == Decision.DENY
        # 相同 arguments + 不同 tool_obj 必须命中同一缓存（指纹不含对象）
        assert len(engine._cache) <= 1, "tool_obj 不同不应产生不同缓存项"

    def test_unmatched_policy_skips(self):
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"action": "delete"}),
                SimpleEffect(Decision.DENY, "no delete"),
            )
        )
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert engine.check(r, ctx).decision == Decision.ALLOW

    def test_cache_eviction(self):
        engine = PolicyEngine()
        engine._CACHE_MAX = 2
        for i in range(5):
            r = Resource(type="tool", name=f"t{i}", action="read")
            ctx = PermissionContext()
            engine.check(r, ctx)
        assert len(engine._cache) <= 2

    def test_cache_key_includes_attributes(self):
        engine = PolicyEngine()
        engine.add(
            ConditionPolicy(
                AttributeCondition({"attr.path": "/safe"}),
                SimpleEffect(Decision.ALLOW, "safe path"),
            )
        )
        engine.add(
            ConditionPolicy(
                AttributeCondition({"attr.path": "/danger"}),
                SimpleEffect(Decision.DENY, "danger path"),
            )
        )
        danger = Resource(type="tool", name="write", action="write", attributes={"path": "/danger"})
        safe = Resource(type="tool", name="write", action="write", attributes={"path": "/safe"})
        ctx = PermissionContext()
        assert engine.check(danger, ctx).decision == Decision.DENY
        assert engine.check(safe, ctx).decision == Decision.ALLOW, (
            "不同 attributes 不得命中同一缓存（误拒）"
        )


class TestAttributeCondition:
    def test_exact_match(self):
        c = AttributeCondition({"resource_type": "tool", "action": "read"})
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert c.match(r, ctx) is True

    def test_type_mismatch(self):
        c = AttributeCondition({"resource_type": "entity"})
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert c.match(r, ctx) is False

    def test_role_match(self):
        c = AttributeCondition({"role": "admin"})
        r = Resource(type="tool", name="any", action="any")
        ctx = PermissionContext(role="admin")
        assert c.match(r, ctx) is True

    def test_attribute_extra(self):
        c = AttributeCondition({"attr.path": "/safe"})
        r = Resource(type="tool", name="write", action="write", attributes={"path": "/safe"})
        ctx = PermissionContext()
        assert c.match(r, ctx) is True

    def test_attribute_mismatch(self):
        c = AttributeCondition({"attr.path": "/safe"})
        r = Resource(type="tool", name="write", action="write", attributes={"path": "/danger"})
        ctx = PermissionContext()
        assert c.match(r, ctx) is False


class TestBuiltinRoles:
    @pytest.mark.parametrize("role_name", ["admin", "power_user", "member", "readonly"])
    def test_all_builtin_roles_exist(self, role_name):
        role = get_builtin_role(role_name)
        assert role is not None
        assert role.name == role_name

    def test_list_roles(self):
        names = list_builtin_roles()
        assert "admin" in names
        assert "member" in names
        assert "readonly" in names

    def test_admin_can_do_anything(self):
        r = Resource(type="tool", name="bash", action="execute")
        ctx = PermissionContext()
        result = check_role(ADMIN, r, ctx)
        assert result.decision == Decision.ALLOW

    def test_readonly_deny_write(self):
        r = Resource(type="tool", name="write", action="write")
        ctx = PermissionContext()
        result = check_role(READONLY, r, ctx)
        assert result.decision == Decision.DENY

    def test_readonly_deny_execute(self):
        r = Resource(type="tool", name="bash", action="execute")
        ctx = PermissionContext()
        result = check_role(READONLY, r, ctx)
        assert result.decision == Decision.DENY

    def test_readonly_read_allowed(self):
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        result = check_role(READONLY, r, ctx)
        assert result.decision == Decision.ALLOW

    def test_member_read_allowed(self):
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        result = check_role(MEMBER, r, ctx)
        assert result.decision == Decision.ALLOW

    def test_member_execute_denied(self):
        r = Resource(type="tool", name="bash", action="execute")
        ctx = PermissionContext()
        result = check_role(MEMBER, r, ctx)
        assert result.decision == Decision.DENY

    def test_power_user_write_ask(self):
        r = Resource(type="tool", name="write", action="write")
        ctx = PermissionContext()
        result = check_role(POWER_USER, r, ctx)
        assert result.decision == Decision.ASK

    def test_unknown_role(self):
        assert get_builtin_role("nonexistent") is None


class TestEntityPolicy:
    def test_non_entity_returns_none(self):
        p = EntityPolicy()
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        assert p.evaluate(r, ctx) is None

    def test_entity_read_allowed(self):
        p = EntityPolicy()
        r = Resource(type="entity", name="project", action="read")
        ctx = PermissionContext()
        result = p.evaluate(r, ctx)
        assert result is not None
        assert result.decision == Decision.ALLOW

    def test_entity_delete_asks(self):
        p = EntityPolicy()
        r = Resource(type="entity", name="project", action="delete")
        ctx = PermissionContext()
        result = p.evaluate(r, ctx)
        assert result is not None
        assert result.decision == Decision.ASK

    def test_entity_type_not_allowed(self):
        p = EntityPolicy(allowed_types={"project", "note"})
        r = Resource(type="entity", name="contact", action="read")
        ctx = PermissionContext()
        result = p.evaluate(r, ctx)
        assert result is not None
        assert result.decision == Decision.DENY

    def test_entity_type_from_attributes(self):
        p = EntityPolicy(allowed_types={"project", "note"})
        allowed = Resource(
            type="entity",
            name="Q3-规划文档",
            action="read",
            attributes={"entity_type": "note"},
        )
        denied = Resource(
            type="entity",
            name="张三",
            action="read",
            attributes={"entity_type": "contact"},
        )
        ctx = PermissionContext()
        assert p.evaluate(allowed, ctx).decision == Decision.ALLOW
        assert p.evaluate(denied, ctx).decision == Decision.DENY

    def test_scope_condition_type_from_attributes(self):
        cond = EntityScopeCondition(allowed_types={"project"})
        r = Resource(
            type="entity",
            name="alpha",
            action="read",
            attributes={"entity_type": "project"},
        )
        ctx = PermissionContext()
        assert cond.match(r, ctx) is True


class TestIntegration:
    def test_engine_with_multiple_builtin_policies(self):
        from bandi.core.permission.builtin import EntityPolicy

        engine = PolicyEngine()
        engine.add(EntityPolicy(allowed_types={"project"}))
        r = Resource(type="entity", name="project", action="read")
        ctx = PermissionContext()
        result = engine.check(r, ctx)
        assert result.decision == Decision.ALLOW

    def test_deny_in_chain_takes_priority(self):
        from bandi.core.permission.builtin import EntityPolicy

        engine = PolicyEngine()
        engine.add(EntityPolicy())
        engine.add(EntityPolicy(allowed_types={"project"}))
        r = Resource(type="entity", name="ghost", action="read")
        ctx = PermissionContext()
        result = engine.check(r, ctx)
        assert result.decision == Decision.DENY

    def test_member_read_tool(self):

        engine = PolicyEngine()
        for p in MEMBER.policies:
            engine.add(p)
        r = Resource(type="tool", name="read", action="read")
        ctx = PermissionContext()
        result = engine.check(r, ctx)
        assert result.decision == Decision.ALLOW
