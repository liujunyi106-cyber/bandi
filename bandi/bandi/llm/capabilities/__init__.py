"""统一能力层 — 每个模型/后端的单一契约。

把模型规格统一到 :class:`CapabilityProfile` + :class:`CapabilityRegistry` 之后
"""

from __future__ import annotations

from bandi.llm.capabilities.adapters import from_model_spec
from bandi.llm.capabilities.bootstrap import bootstrap_capabilities, register_model_capabilities
from bandi.llm.capabilities.profile import (
    BackendClass,
    CapabilityContract,
    CapabilityProfile,
    Modality,
    TaskType,
)
from bandi.llm.capabilities.provider_stats import (
    ProviderStat,
    ProviderStatsStore,
    get_provider_stats,
    reset_provider_stats,
)
from bandi.llm.capabilities.registry import (
    CapabilityRegistry,
    RegisteredCapability,
    get_capability_registry,
    reset_capability_registry,
)
from bandi.llm.capabilities.router import CapabilityRouter

__all__ = [
    "BackendClass",
    "CapabilityContract",
    "CapabilityProfile",
    "CapabilityRegistry",
    "CapabilityRouter",
    "Modality",
    "ProviderStat",
    "ProviderStatsStore",
    "RegisteredCapability",
    "TaskType",
    "bootstrap_capabilities",
    "from_model_spec",
    "get_capability_registry",
    "get_provider_stats",
    "register_model_capabilities",
    "reset_capability_registry",
    "reset_provider_stats",
]
