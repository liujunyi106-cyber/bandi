"""契约：OpenAI 兼容 provider 家族（moonshot/zhipu/minimax/mimo/ollama）构造行为。

@base: bandi/llm/registry.py `_get_or_create` + bandi/llm/providers/openai.py
背景（二次核实批）：注册表对 compat 家族按名实例化（不传 base_url 参数）
而 OpenAICompatProvider 构造签名曾为必填 base_url ⇒ 激活 moonshot/zhipu 档案必 TypeError。
本契约钉死不变式：
1) 家族任一注册名均可按名实例化（缺省路径不得 TypeError）；
2) base_url 解析顺序 = 构造显式参数 > config.llm.base_url；
3) 目录内全部 provider 均在注册表注册（防「目录有模型、provider 未注册」）。
"""

from __future__ import annotations

import pytest

from bandi.core.config import BandiConfig
from bandi.core.exceptions import LLMError
from bandi.llm.providers.openai import OpenAICompatProvider
from bandi.llm.registry import ProviderRegistry

COMPAT_FAMILY = ("openai_compat", "moonshot", "zhipu", "minimax", "mimo", "ollama")


def _registry() -> ProviderRegistry:
    config = BandiConfig()
    config.llm.api_key = "test-key-placeholder"
    return ProviderRegistry(config)


def test_compat_family_registered_and_instantiable() -> None:
    registry = _registry()
    config = BandiConfig()
    config.llm.api_key = "test-key-placeholder"
    for name in COMPAT_FAMILY:
        provider_cls = registry._providers.get(name)
        assert provider_cls is not None, f"{name} 未注册"
        assert issubclass(provider_cls, OpenAICompatProvider)
        # 破坏者路径：按名实例化（不传 base_url 参数）不得 TypeError
        provider = registry._get_or_create("contract-probe-model", name, config)
        assert isinstance(provider, OpenAICompatProvider)


def test_compat_family_base_url_resolution_order() -> None:
    """显式参数 > config.llm.base_url（缺省回退）——两条路径都必须能构造。"""
    config = BandiConfig()
    config.llm.base_url = "https://config.example.com/v1"
    explicit = OpenAICompatProvider(config.llm, "https://explicit.example.com/v1")
    assert explicit._base_url == "https://explicit.example.com/v1"
    fallback = OpenAICompatProvider(config.llm)
    assert fallback._base_url == "https://config.example.com/v1"


def test_catalog_providers_all_registered() -> None:
    from bandi.llm.model_catalog import BUILTIN_MODELS

    registry = _registry()
    for name, spec in BUILTIN_MODELS.items():
        if spec.provider == "mock":
            continue
        assert spec.provider in registry._providers, (
            f"{name} 归属 provider '{spec.provider}' 未在注册表注册"
        )


def test_unknown_provider_falls_back_to_compat_with_base_url() -> None:
    """自定义网关（前端「自定义…」档案语义）：任意 provider 标识 + base_url
    ⇒ OpenAI 兼容协议兜底；无 base_url 则 fail-closed。"""
    config = BandiConfig()
    config.llm.provider = "openrouter"
    config.llm.base_url = "https://openrouter.ai/api/v1"
    config.llm.api_key = "sk-x"
    registry = _registry()
    provider = registry.get_provider("x-ai/grok-4", config)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._base_url == "https://openrouter.ai/api/v1"

    no_url = BandiConfig()
    no_url.llm.provider = "openrouter"
    with pytest.raises(LLMError, match="not registered"):
        registry.get_provider("x-ai/grok-4", no_url)
