# 功能：模型规格到能力画像适配（from_model_spec 转换）
"""把既有描述符提升为 :class:`CapabilityProfile`。

Bandi 当前只有一个能力来源 — :class:`~bandi.llm.model_catalog.ModelSpec`（chat 模型）。
func:`from_model_spec` 是让旧体系喂入统一注册表而不重写它的桥接层。
"""

from __future__ import annotations

from typing import Any

from bandi.core.security.secret_protect import unprotect
from bandi.llm.capabilities.profile import BackendClass, CapabilityProfile, Modality, TaskType


def _provider_credentials_available(provider: str) -> bool:
    """契约3 行为修复：按 provider 检查凭证是否可用——api_key/base_url 来自
    env（BANDI_LLM_API_KEY/OPENAI_API_KEY/BANDI_LLM_BASE_URL）或 config
    （llm.api_key/llm.base_url）。mock 恒可用；openai_compat 需要 key + base_url；
    """
    import os

    if provider == "mock":
        return True
    # 凭证与请求侧同源：只认 BANDI_LLM_API_KEY（BandiConfig 读取的 env）+
    # config——OPENAI_API_KEY 不被请求侧读取，据此判可用会放行未配凭证模型
    # ④ env 值可能是 dpapi: 密文（与 config 同口径解密，防密文被判为可用凭证）
    env_key = unprotect(os.environ.get("BANDI_LLM_API_KEY", ""))
    env_base = os.environ.get("BANDI_LLM_BASE_URL", "")
    cfg_key = ""
    cfg_base = ""
    try:
        from bandi.core.config import BandiConfig

        llm = BandiConfig.load().llm
        cfg_key = llm.api_key or ""
        cfg_base = llm.base_url or ""
    except Exception:  # noqa: BLE001
        pass
    api_key = env_key or cfg_key
    if provider in ("deepseek", "openai", "anthropic", "gemini"):
        return bool(api_key)
    if provider == "openai_compat":
        base_url = env_base or cfg_base
        return bool(api_key and base_url)
    return bool(api_key)


def from_model_spec(spec: Any) -> CapabilityProfile:
    """从 :class:`~bandi.llm.model_catalog.ModelSpec` 构建能力档案。

    - 所有 Bandi 模型为 chat 类；
    - ``supports_vision`` → 输入加 IMAGE、任务加 VISION、类别升级为 MULTIMODAL_LLM；
    - ``mock`` → OFFLINE 兜底（cost_tier=0、无需凭证）；
      不过滤任何模型 → 候选链含全部 30+ 模型，未配凭证的模型逐个网络级失败。
    """
    name = spec.name
    provider = spec.provider
    supports_vision = bool(getattr(spec, "supports_vision", False))
    supports_streaming = bool(getattr(spec, "supports_streaming", True))
    context_window = int(getattr(spec, "context_window", 0) or 0)

    if provider == "mock":
        backend_class = BackendClass.OFFLINE
        cost_tier = 0
        requires_credentials = False
    elif supports_vision:
        backend_class = BackendClass.MULTIMODAL_LLM
        cost_tier = 2
        requires_credentials = True
    else:
        backend_class = BackendClass.TEXT_LLM
        cost_tier = 2
        requires_credentials = True

    inputs = {Modality.TEXT}
    tasks = {TaskType.CHAT}
    if supports_vision:
        inputs.add(Modality.IMAGE)
        tasks.add(TaskType.VISION)

    return CapabilityProfile(
        id=f"chat:{provider}:{name}",
        provider=provider,
        backend_class=backend_class,
        inputs=frozenset(inputs),
        outputs=frozenset({Modality.TEXT}),
        tasks=frozenset(tasks),
        default_model=name,
        requires_credentials=requires_credentials,
        supports_streaming=supports_streaming,
        cost_tier=cost_tier,
        availability=lambda: _provider_credentials_available(provider),
        metadata={
            "tool_call": bool(getattr(spec, "supports_tool_calling", True)),
            "context_window": context_window,
            "reasoning": bool(getattr(spec, "supports_reasoning", False)),
            "reasoning_efforts": list(getattr(spec, "reasoning_efforts", [])),
        },
    )


__all__ = ["from_model_spec"]
