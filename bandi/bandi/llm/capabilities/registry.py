# 功能：能力注册表（能力登记/查询/全局单例）
"""``CapabilityRegistry`` — 能力档案的单一事实来源。

每个模型/后端被提升为一个 :class:`CapabilityProfile` 注册于此，可选挂载 *invoker*
（真正执行它的底层对象）。路由器和 fallback 通过本注册表查询，而非维护自己的
硬编码 Provider 列表。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bandi.llm.capabilities.profile import (
    BackendClass,
    CapabilityContract,
    CapabilityProfile,
    Modality,
    TaskType,
)

Invoker = Callable[..., object]


@dataclass
class RegisteredCapability:
    """一个 profile 与其（可选）invoker 句柄。"""

    profile: CapabilityProfile
    invoker: Invoker | None = None


class CapabilityRegistry:
    """进程内（或作用域内）的能力档案注册表。"""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredCapability] = {}

    def register(
        self,
        profile: CapabilityProfile,
        invoker: Invoker | None = None,
        *,
        replace: bool = True,
    ) -> None:
        if profile.id in self._entries and not replace:
            raise ValueError(f"Capability '{profile.id}' is already registered")
        self._entries[profile.id] = RegisteredCapability(profile=profile, invoker=invoker)

    def unregister(self, profile_id: str) -> None:
        self._entries.pop(profile_id, None)

    def get(self, profile_id: str) -> CapabilityProfile | None:
        entry = self._entries.get(profile_id)
        return entry.profile if entry else None

    def get_invoker(self, profile_id: str) -> Invoker | None:
        entry = self._entries.get(profile_id)
        return entry.invoker if entry else None

    def all(self) -> list[CapabilityProfile]:
        return [e.profile for e in self._entries.values()]

    def providers(self) -> list[str]:
        seen: list[str] = []
        for e in self._entries.values():
            if e.profile.provider not in seen:
                seen.append(e.profile.provider)
        return seen

    def query(
        self,
        *,
        task: TaskType | str | None = None,
        contract: CapabilityContract | None = None,
        input: Modality | str | None = None,
        output: Modality | str | None = None,
        provider: str | None = None,
        backend_class: BackendClass | None = None,
        available_only: bool = False,
    ) -> list[CapabilityProfile]:
        """按给定过滤条件返回匹配的 profile，保持注册顺序。"""
        results: list[CapabilityProfile] = []
        for entry in self._entries.values():
            p = entry.profile
            if contract is not None and not contract.matches(p):
                continue
            if task is not None and not p.supports_task(task):
                continue
            if input is not None and not p.supports_input(input):
                continue
            if output is not None and not p.supports_output(output):
                continue
            if provider is not None and p.provider != provider:
                continue
            if backend_class is not None and p.backend_class != backend_class:
                continue
            if available_only and not p.available():
                continue
            results.append(p)
        return results

    def clear(self) -> None:
        self._entries.clear()


_REGISTRY: CapabilityRegistry | None = None


def get_capability_registry() -> CapabilityRegistry:
    """返回进程级能力注册表（懒创建单例）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CapabilityRegistry()
    return _REGISTRY


def reset_capability_registry() -> None:
    """重置进程级注册表（测试辅助）。"""
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "CapabilityRegistry",
    "RegisteredCapability",
    "get_capability_registry",
    "reset_capability_registry",
]
