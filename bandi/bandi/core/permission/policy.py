# 功能：权限策略定义（条件/效果/策略匹配）
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from bandi.core.permission.models import Decision, PermissionContext, PermissionDecision, Resource


class Condition(ABC):
    @abstractmethod
    def match(self, resource: Resource, ctx: PermissionContext) -> bool: ...


class Effect(ABC):
    @abstractmethod
    def evaluate(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision | None: ...


class Policy(ABC):
    @abstractmethod
    def evaluate(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision | None: ...


class AttributeCondition(Condition):
    def __init__(self, expected: dict[str, Any]) -> None:
        self._expected = expected

    def match(self, resource: Resource, ctx: PermissionContext) -> bool:
        for key, val in self._expected.items():
            if key == "resource_type":
                if resource.type != val:
                    return False
                continue
            if key == "resource_name":
                if resource.name != val:
                    return False
                continue
            if key == "action":
                if resource.action != val:
                    return False
                continue
            if key == "role":
                if ctx.role != val:
                    return False
                continue
            if key == "mode":
                if ctx.mode != val:
                    return False
                continue
            if key.startswith("attr."):
                attr_key = key[5:]
                if resource.attributes.get(attr_key) != val:
                    return False
                continue
            # 拼错键（如 resorce_type）恒真放行（安全策略失效）
            return False
        return True


class SimpleEffect(Effect):
    def __init__(self, decision: Decision, reason: str = "") -> None:
        self._decision = decision
        self._reason = reason

    def evaluate(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision | None:
        return PermissionDecision(decision=self._decision, reason=self._reason)


class ConditionPolicy(Policy):
    def __init__(self, condition: Condition, effect: Effect, name: str = "") -> None:
        self._condition = condition
        self._effect = effect
        self._name = name

    def evaluate(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision | None:
        if self._condition.match(resource, ctx):
            return self._effect.evaluate(resource, ctx)
        return None
