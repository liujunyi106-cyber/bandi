# 功能：能力引导（从模型目录/生成服务自动注册能力画像）
"""向进程级能力注册表填充全部模型能力。

这是能力驱动路由 / fallback 的发现面：从 :class:`~bandi.llm.model_catalog.ModelCatalog`
把每个模型规格提升为 :class:`CapabilityProfile` 并注册。媒体生成/语音类任务（ADR-33）
由 :func:`register_generation_capabilities` 注册离线兜底画像（invoker 由
class:`~bandi.generation.service.GenerationService` 挂载）。
"""

from __future__ import annotations

import logging
from typing import Any

from bandi.llm.capabilities.adapters import from_model_spec
from bandi.llm.capabilities.registry import CapabilityRegistry, get_capability_registry

logger = logging.getLogger(__name__)


def register_model_capabilities(
    catalog: Any,
    registry: CapabilityRegistry | None = None,
) -> list[str]:
    """把目录中的模型规格注册进 *registry*。

    ``catalog`` 是 :class:`~bandi.llm.model_catalog.ModelCatalog`（或任何暴露
    ``list_models()`` 的对象）。
    """
    registry = registry or get_capability_registry()
    registered: list[str] = []
    specs = _iter_specs(catalog)
    for spec in specs:
        try:
            profile = from_model_spec(spec)
            registry.register(profile)
            registered.append(profile.id)
        except Exception as e:  # noqa: BLE001 - 跳过畸形规格
            logger.debug("skip malformed model spec: %s", e)
            continue
    return registered


def bootstrap_capabilities(
    catalog: Any | None = None,
    registry: CapabilityRegistry | None = None,
) -> list[str]:
    """把可选目录的模型能力注册进注册表，返回注册的 profile id 列表。"""
    registry = registry or get_capability_registry()
    if catalog is None:
        return []
    return register_model_capabilities(catalog, registry=registry)


def register_generation_capabilities(
    registry: CapabilityRegistry | None = None,
) -> list[str]:
    """注册媒体生成的离线兜底画像（image/audio/video）。

    离线后端始终可用、无需凭证，作为远程 provider 缺失时的兜底地板。
    """
    from bandi.generation.adapters import from_generation_backend
    from bandi.generation.offline import OfflineGenerationBackend

    registry = registry or get_capability_registry()
    registered: list[str] = []
    offline = OfflineGenerationBackend()
    for profile in from_generation_backend(offline):
        registry.register(profile)
        registered.append(profile.id)
    return registered


def _iter_specs(catalog: Any) -> list[Any]:
    for attr in ("all_specs", "specs", "list_models"):
        fn = getattr(catalog, attr, None)
        if callable(fn):
            try:
                return list(fn())
            except Exception as e:  # noqa: BLE001
                logger.debug("provider profile discovery failed: %s", e)
                continue
    return []


__all__ = [
    "bootstrap_capabilities",
    "register_generation_capabilities",
    "register_model_capabilities",
]
