# 功能：内置角色定义（获取/列出/校验内置角色）
from __future__ import annotations

from dataclasses import dataclass, field

from bandi.core.permission.models import Decision, PermissionContext, PermissionDecision, Resource
from bandi.core.permission.policy import (
    AttributeCondition,
    ConditionPolicy,
    Policy,
    SimpleEffect,
)
from bandi.core.permission.policy_engine import PolicyEngine


@dataclass
class Role:
    name: str
    policies: list[Policy] = field(default_factory=list)


ADMIN: Role = Role(
    name="admin",
    policies=[
        ConditionPolicy(
            AttributeCondition({}),
            SimpleEffect(Decision.ALLOW, "admin: full access"),
            name="admin_allow_all",
        ),
    ],
)

POWER_USER: Role = Role(
    name="power_user",
    policies=[
        ConditionPolicy(
            AttributeCondition({"action": "read"}),
            SimpleEffect(Decision.ALLOW, "power_user: read allowed"),
            name="power_user_read",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "write"}),
            SimpleEffect(Decision.ASK, "power_user: write needs approval"),
            name="power_user_write_ask",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "delete"}),
            SimpleEffect(Decision.ASK, "power_user: delete needs approval"),
            name="power_user_delete_ask",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "execute"}),
            SimpleEffect(Decision.ASK, "power_user: execute needs approval"),
            name="power_user_execute_ask",
        ),
    ],
)

MEMBER: Role = Role(
    name="member",
    policies=[
        ConditionPolicy(
            AttributeCondition({"action": "read"}),
            SimpleEffect(Decision.ALLOW, "member: read allowed"),
            name="member_read",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "write"}),
            SimpleEffect(Decision.ASK, "member: write needs approval"),
            name="member_write_ask",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "delete"}),
            SimpleEffect(Decision.ASK, "member: delete needs approval"),
            name="member_delete_ask",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "execute"}),
            SimpleEffect(Decision.DENY, "member: execute denied"),
            name="member_execute_deny",
        ),
    ],
)

READONLY: Role = Role(
    name="readonly",
    policies=[
        ConditionPolicy(
            AttributeCondition({"action": "read"}),
            SimpleEffect(Decision.ALLOW, "readonly: read allowed"),
            name="readonly_read",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "write"}),
            SimpleEffect(Decision.DENY, "readonly: write denied"),
            name="readonly_write_deny",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "delete"}),
            SimpleEffect(Decision.DENY, "readonly: delete denied"),
            name="readonly_delete_deny",
        ),
        ConditionPolicy(
            AttributeCondition({"action": "execute"}),
            SimpleEffect(Decision.DENY, "readonly: execute denied"),
            name="readonly_execute_deny",
        ),
    ],
)

_BUILTIN: dict[str, Role] = {
    "admin": ADMIN,
    "power_user": POWER_USER,
    "member": MEMBER,
    "readonly": READONLY,
}


# 预留：内置角色访问器；启用时机 user 角色管理面
def get_builtin_role(name: str) -> Role | None:
    return _BUILTIN.get(name)


def list_builtin_roles() -> list[str]:
    return list(_BUILTIN)


# 预留：角色策略独立检查入口（管理面用）；启用时机 user 角色管理面
def check_role(role: Role, resource: Resource, ctx: PermissionContext) -> PermissionDecision:
    engine = PolicyEngine()
    for p in role.policies:
        engine.add(p)
    return engine.check(resource, ctx)


def register_builtin_role(engine: PolicyEngine, name: str) -> bool:
    """/Day124 主线接线：按名把内置角色策略注册进引擎（选择性 add）。

    未知角色（如默认 "user"）返回 False 且不注册任何策略——保持权限管道
    （PermissionMiddleware 的 pp.check）单链现状；admin_allow_all 等强语义仅在
    显式声明该角色时生效。
    """
    role = get_builtin_role(name)
    if role is None:
        return False
    for p in role.policies:
        engine.add(p)
    return True
