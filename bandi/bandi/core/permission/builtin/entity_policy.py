# 功能：内置实体范围策略（实体过滤条件 + 实体级权限裁决）
from __future__ import annotations

from bandi.core.permission.models import Decision, PermissionContext, PermissionDecision, Resource
from bandi.core.permission.policy import Condition, Policy


class EntityScopeCondition(Condition):
    def __init__(self, allowed_types: set[str] | None = None) -> None:
        self._allowed = allowed_types

    def match(self, resource: Resource, ctx: PermissionContext) -> bool:
        if resource.type != "entity":
            return False
        if self._allowed and self._entity_type(resource) not in self._allowed:
            return False
        return True

    @staticmethod
    def _entity_type(resource: Resource) -> str:
        return resource.attributes.get("entity_type", resource.name)


class EntityPolicy(Policy):
    def __init__(self, allowed_types: set[str] | None = None) -> None:
        self._allowed = allowed_types

    def evaluate(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision | None:
        if resource.type != "entity":
            return None
        entity_type = resource.attributes.get("entity_type", resource.name)
        if self._allowed and entity_type not in self._allowed:
            return PermissionDecision(
                Decision.DENY,
                reason=f"entity type '{entity_type}' not allowed",
            )
        if resource.action in ("delete",):
            return PermissionDecision(
                Decision.ASK,
                reason=f"delete entity '{resource.name}' needs approval",
            )
        return PermissionDecision(Decision.ALLOW)
