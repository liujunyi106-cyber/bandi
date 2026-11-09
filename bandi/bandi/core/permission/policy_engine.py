# 功能：权限策略引擎（策略注册与裁决）
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from bandi.core.permission.models import Decision, PermissionContext, PermissionDecision, Resource
from bandi.core.permission.policy import Policy


class PolicyEngine:
    def __init__(self) -> None:
        self._policies: list[Policy] = []
        self._cache: OrderedDict[str, PermissionDecision] = OrderedDict()
        self._CACHE_MAX = 128

    @staticmethod
    def _attributes_fingerprint(attributes: dict | None) -> str:
        if not attributes:
            return ""
        # 排除不可序列化对象键（tool_obj 含内存地址，default=str 后指纹每次不同
        # → 缓存恒 miss）。仅用可序列化字段参与指纹。
        serializable = {k: v for k, v in attributes.items() if k != "tool_obj"}
        raw = json.dumps(serializable, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def add(self, policy: Policy) -> None:
        self._policies.append(policy)
        self._cache.clear()

    def remove(self, policy: Policy) -> None:
        self._policies.remove(policy)
        self._cache.clear()

    def check(self, resource: Resource, ctx: PermissionContext) -> PermissionDecision:
        cache_key = (
            f"{resource.type}:{resource.name}:{resource.action}:{ctx.role}:{ctx.mode}"
            f":{self._attributes_fingerprint(resource.attributes)}"
        )
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        has_ask = False
        ask_reason = ""
        for policy in self._policies:
            result = policy.evaluate(resource, ctx)
            if result is None:
                continue
            if result.decision == Decision.DENY:
                self._cache[cache_key] = result
                if len(self._cache) > self._CACHE_MAX:
                    self._cache.popitem(last=False)
                return result
            if result.decision == Decision.ASK:
                has_ask = True
                ask_reason = result.reason or ask_reason
        if has_ask:
            result = PermissionDecision(decision=Decision.ASK, reason=ask_reason)
            self._cache[cache_key] = result
            if len(self._cache) > self._CACHE_MAX:
                self._cache.popitem(last=False)
            return result

        result = PermissionDecision(decision=Decision.ALLOW)
        self._cache[cache_key] = result
        if len(self._cache) > self._CACHE_MAX:
            self._cache.popitem(last=False)
        return result
