import pytest

from bandi.core.config import BandiConfig
from bandi.core.exceptions import LLMError
from bandi.llm.mock import MockProvider
from bandi.llm.registry import ProviderRegistry


def test_register_and_resolve_provider():
    registry = ProviderRegistry()
    registry.register("mock", MockProvider)
    registry.register_model("mock-model", "mock")
    config = BandiConfig()
    provider = registry.get_provider("mock-model", config)
    assert isinstance(provider, MockProvider)


def test_resolve_unknown_model():
    registry = ProviderRegistry()
    config = BandiConfig()
    with pytest.raises(LLMError, match="No provider for model 'unknown' and no base_url set"):
        registry.get_provider("unknown", config)


def test_resolve_unregistered_provider():
    registry = ProviderRegistry()
    registry.register_model("test-model", "nonexistent")
    config = BandiConfig()
    with pytest.raises(LLMError, match="Provider 'nonexistent' not registered"):
        registry.get_provider("test-model", config)


def test_resolve_caches_instance():
    registry = ProviderRegistry()
    registry.register("mock", MockProvider)
    registry.register_model("m1", "mock")
    config = BandiConfig()
    p1 = registry.get_provider("m1", config)
    p2 = registry.get_provider("m1", config)
    assert p1 is p2


def test_available_models():
    registry = ProviderRegistry()
    registry.register_model("a", "mock")
    registry.register_model("b", "mock")
    models = registry.available_models()
    assert "a" in models
    assert "b" in models


def test_default_model_deepseek_flash_registered():
    registry = ProviderRegistry(BandiConfig())
    config = BandiConfig()
    provider_name = registry._model_to_provider.get("deepseek-v4-flash")
    assert provider_name == "deepseek", "deepseek-v4-flash 必须自动映射到 deepseek provider"
    provider = registry.get_provider("deepseek-v4-flash", config)
    assert provider is not None


def test_catalog_models_all_registered_single_source():
    from bandi.llm.model_catalog import BUILTIN_MODELS

    registry = ProviderRegistry(BandiConfig())
    for model_name, spec in BUILTIN_MODELS.items():
        if spec.provider == "mock":
            continue
        assert model_name in registry._model_to_provider, (
            f"{model_name} 未注册（catalog 有、registry 缺）"
        )
        assert registry._model_to_provider[model_name] == spec.provider


def test_default_config_only_api_key_builds_provider():
    import os

    os.environ["BANDI_LLM_API_KEY"] = "test-key-placeholder"
    try:
        config = BandiConfig()
        registry = ProviderRegistry(config)
        provider = registry.get_provider(config.llm.default_model, config)
        assert provider is not None
    finally:
        os.environ.pop("BANDI_LLM_API_KEY", None)


def test_cache_key_includes_provider_name():
    """
    热切换（openai ↔ openai_compat ↔ gemini 同挂 base_url）时不得返回旧实例。"""
    from bandi.core.config import BandiConfig
    from bandi.llm.mock import MockProvider

    registry = ProviderRegistry()
    registry.register("mock_a", MockProvider)
    registry.register("mock_b", MockProvider)
    registry.register_model("m1", "mock_a")

    config_a = BandiConfig()
    config_a.llm.provider = "mock_a"
    config_a.llm.base_url = "https://shared.example.com"
    config_b = BandiConfig()
    config_b.llm.provider = "mock_b"
    config_b.llm.base_url = "https://shared.example.com"

    p_a = registry.get_provider("m1", config_a)
    p_b = registry.get_provider("m1", config_b)
    assert p_a is not p_b, "同一 model+base_url 但 provider 不同必须返回不同实例"


def test_same_provider_reuses_cache():
    from bandi.core.config import BandiConfig
    from bandi.llm.mock import MockProvider

    registry = ProviderRegistry()
    registry.register("mock_a", MockProvider)
    registry.register_model("m1", "mock_a")

    config = BandiConfig()
    config.llm.provider = "mock_a"
    config.llm.base_url = "https://shared.example.com"
    p1 = registry.get_provider("m1", config)
    p2 = registry.get_provider("m1", config)
    assert p1 is p2


def test_compat_family_providers_resolve_without_base_url_arg():
    """回归：compat 家族（moonshot/zhipu/minimax/mimo）按名实例化——
    """
    from bandi.llm.providers.openai import OpenAICompatProvider

    config = BandiConfig()
    config.llm.api_key = "test-key-placeholder"
    registry = ProviderRegistry(config)

    assert isinstance(registry.get_provider("kimi-k3", config), OpenAICompatProvider)
    assert isinstance(registry.get_provider("glm-5.3", config), OpenAICompatProvider)
    assert isinstance(registry.get_provider("MiniMax-M3", config), OpenAICompatProvider)
    assert isinstance(registry.get_provider("mimo-v2.6-pro", config), OpenAICompatProvider)


def test_ollama_provider_resolves_via_explicit_provider():
    """Ollama 本地模型无目录条目——显式 provider + base_url 通道可解析。"""
    config = BandiConfig()
    config.llm.api_key = "ollama"
    config.llm.provider = "ollama"
    config.llm.base_url = "http://127.0.0.1:11434/v1"
    registry = ProviderRegistry(config)
    provider = registry.get_provider("qwen3:8b", config)
    assert provider is not None
