from bandi.llm.model_catalog import BUILTIN_MODELS, ModelCatalog, ModelPricing, ModelSpec


def test_builtin_models_count():
    assert len(BUILTIN_MODELS) >= 4


def test_registry_models_covered_by_catalog():
    from bandi.llm.registry import ProviderRegistry

    reg = ProviderRegistry()
    missing = [m for m in reg.available_models() if m not in BUILTIN_MODELS]
    assert missing == [], f"registry 模型目录缺失: {missing}"
    assert "deepseek-v4-flash" in BUILTIN_MODELS
    assert BUILTIN_MODELS["deepseek-v4-flash"].pricing.input_per_1m > 0


def test_deepseek_flash_spec():
    """：deepseek-chat/reasoner 已于 下线 ⇒ 断言现役 v4-flash。"""
    spec = BUILTIN_MODELS["deepseek-v4-flash"]
    assert spec.provider == "deepseek"
    assert spec.context_window == 1_000_000
    assert spec.supports_tool_calling is True
    assert spec.pricing.input_per_1m > 0
    assert spec.pricing.output_per_1m > spec.pricing.input_per_1m


def test_mock_model():
    spec = BUILTIN_MODELS["mock"]
    assert spec.provider == "mock"
    assert spec.pricing.input_per_1m == 0
    assert spec.pricing.output_per_1m == 0


def test_catalog_get():
    catalog = ModelCatalog()
    spec = catalog.get("deepseek-v4-flash")
    assert spec is not None
    assert spec.name == "deepseek-v4-flash"

    assert catalog.get("nonexistent") is None


def test_catalog_list_by_provider():
    catalog = ModelCatalog()
    deepseek_models = catalog.list_models(provider="deepseek")
    names = [m.name for m in deepseek_models]
    assert "deepseek-v4-flash" in names
    assert "deepseek-v4-pro" in names

    openai_models = catalog.list_models(provider="openai")
    names = [m.name for m in openai_models]
    assert "gpt-5.2" in names
    assert "gpt-4.1" in names

    assert [m.name for m in catalog.list_models(provider="moonshot")]
    assert [m.name for m in catalog.list_models(provider="zhipu")]


def test_catalog_list_all():
    catalog = ModelCatalog()
    all_models = catalog.list_models()
    assert len(all_models) >= 4


def test_catalog_register():
    catalog = ModelCatalog()
    custom = ModelSpec(name="custom-model", provider="custom", context_window=4096)
    catalog.register(custom)
    assert catalog.get("custom-model") is custom


def test_catalog_contains():
    catalog = ModelCatalog()
    assert "deepseek-v4-flash" in catalog
    assert "nonexistent" not in catalog


def test_catalog_custom_models_override():
    custom = ModelSpec(name="deepseek-v4-flash", provider="custom", context_window=999)
    catalog = ModelCatalog(models={"deepseek-v4-flash": custom})
    spec = catalog.get("deepseek-v4-flash")
    assert spec.provider == "custom"
    assert spec.context_window == 999


def test_model_pricing_defaults():
    p = ModelPricing(1.5, 3.0)
    assert p.currency == "CNY"


def test_model_spec_defaults():
    spec = ModelSpec(name="test", provider="test", context_window=4096)
    assert spec.max_output_tokens == 4096
    assert spec.supports_tool_calling is True
    assert spec.supports_vision is False
    assert spec.supports_streaming is True


def test_catalog_refresh_2026_09_openai_deepseek():
    """二次核实：GPT-6 Astra / GPT-5.6 三件套 / DeepSeek .1（deepseek-flash）入目录。"""
    for name in ("gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "deepseek-flash"):
        spec = BUILTIN_MODELS[name]
        assert spec.pricing.input_per_1m > 0
        assert spec.pricing.output_per_1m > spec.pricing.input_per_1m
    assert BUILTIN_MODELS["gpt-6-astra"].provider == "openai"
    assert BUILTIN_MODELS["gpt-5.6-terra"].provider == "openai"
    assert BUILTIN_MODELS["deepseek-flash"].provider == "deepseek"
    assert BUILTIN_MODELS["deepseek-flash"].supports_vision is True
    assert BUILTIN_MODELS["deepseek-flash"].context_window == 1_000_000


def test_catalog_refresh_2026_09_minimax_mimo():
    """二次核实：MiniMax（.7）与小米 MiMo（v2.6）独立 provider 入目录。"""
    assert BUILTIN_MODELS["MiniMax-M3"].provider == "minimax"
    assert BUILTIN_MODELS["MiniMax-M3"].context_window == 1_000_000
    assert BUILTIN_MODELS["MiniMax-M2.7"].provider == "minimax"
    assert BUILTIN_MODELS["mimo-v2.6-pro"].provider == "mimo"
    assert BUILTIN_MODELS["mimo-v2.6-flash"].provider == "mimo"
    assert BUILTIN_MODELS["mimo-v2.6-pro"].supports_vision is True
