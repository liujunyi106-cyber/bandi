from __future__ import annotations

import pytest

from bandi.core.config import BandiConfig
from bandi.llm.capabilities.adapters import from_model_spec
from bandi.llm.capabilities.bootstrap import bootstrap_capabilities
from bandi.llm.capabilities.profile import (
    BackendClass,
    CapabilityContract,
    CapabilityProfile,
    Modality,
    TaskType,
)
from bandi.llm.capabilities.provider_stats import (
    NEUTRAL_SCORE,
    ProviderStatsStore,
    get_provider_stats,
    reset_provider_stats,
)
from bandi.llm.capabilities.registry import (
    CapabilityRegistry,
    get_capability_registry,
    reset_capability_registry,
)
from bandi.llm.capabilities.router import CapabilityRouter
from bandi.llm.model_catalog import BUILTIN_MODELS, ModelCatalog, ModelSpec


def _chat_profile(
    profile_id: str = "chat:test:model-a",
    provider: str = "test",
    cost_tier: int = 2,
    **kwargs,
) -> CapabilityProfile:
    return CapabilityProfile(
        id=profile_id,
        provider=provider,
        backend_class=BackendClass.TEXT_LLM,
        inputs=frozenset({Modality.TEXT}),
        outputs=frozenset({Modality.TEXT}),
        tasks=frozenset({TaskType.CHAT}),
        default_model="model-a",
        cost_tier=cost_tier,
        **kwargs,
    )


class TestProfile:
    def test_task_type_includes_media_generation(self) -> None:
        for name in (
            "IMAGE_GEN",
            "VIDEO_GEN",
            "MESH_GEN",
            "VFX_GEN",
            "AUDIO_GEN",
            "UPSCALE",
            "TTS",
            "ASR",
            "EMBEDDING",
        ):
            assert TaskType[name] is not None

    def test_supports_task_and_modality(self) -> None:
        p = _chat_profile()
        assert p.supports_task(TaskType.CHAT)
        assert p.supports_task("chat")
        assert not p.supports_task(TaskType.EMBEDDING)
        assert p.supports_input("text")
        assert not p.supports_input("image")

    def test_available_with_callable(self) -> None:
        assert _chat_profile().available() is True
        assert _chat_profile(availability=lambda: False).available() is False
        assert _chat_profile(availability=lambda: 1 / 0).available() is False

    def test_to_dict_shape(self) -> None:
        d = _chat_profile().to_dict()
        assert d["id"] == "chat:test:model-a"
        assert d["backend_class"] == "text_llm"
        assert d["inputs"] == ["text"]
        assert d["tasks"] == ["chat"]
        assert d["available"] is True

    def test_profile_is_frozen(self) -> None:
        p = _chat_profile()
        with pytest.raises(Exception):
            p.provider = "changed"


class TestAvailabilityWiring:
    """
    router.available_only=True 不过滤任何模型 → 候选链含全部 30+ 模型，用户
    只配 deepseek key 时逐个网络级失败（慢且吵）。"""

    def _profile_available(self, spec: ModelSpec, api_key: str = "", base_url: str = "") -> bool:
        import os

        from bandi.llm.capabilities.adapters import from_model_spec

        env = dict(os.environ)
        try:
            os.environ["BANDI_LLM_API_KEY"] = api_key
            if base_url:
                os.environ["BANDI_LLM_BASE_URL"] = base_url
            else:
                os.environ.pop("BANDI_LLM_BASE_URL", None)
            profile = from_model_spec(spec)
            return profile.available()
        finally:
            os.environ.clear()
            try:
                os.environ.update(env)
            except ValueError:
                # 宿主可能注入超长环境变量（>32767，Windows 上限，如沙箱的
                # ACC_PRODUCT_CONFIG_V3）——恢复失败不致命，保留当前环境即可。
                pass

    def test_deepseek_requires_api_key(self) -> None:
        spec = BUILTIN_MODELS["deepseek-v4-flash"]
        assert self._profile_available(spec, api_key="") is False, "无 key 不可用"
        assert self._profile_available(spec, api_key="sk-test") is True, "有 key 可用"

    def test_openai_compat_requires_key_and_base_url(self) -> None:
        # 不再依赖目录里存在该 provider 的模型
        spec = ModelSpec(name="custom-gateway", provider="openai_compat", context_window=32_000)
        assert self._profile_available(spec) is False, "无 key 无 base_url 不可用"
        assert self._profile_available(spec, api_key="k", base_url="https://x") is True

    def test_anthropic_requires_api_key(self) -> None:
        spec = BUILTIN_MODELS["claude-sonnet-5"]
        assert self._profile_available(spec, api_key="") is False
        assert self._profile_available(spec, api_key="sk-ant") is True

    def test_mock_always_available(self) -> None:
        spec = BUILTIN_MODELS["mock"]
        assert self._profile_available(spec) is True, "mock 无需凭证"


class TestContract:
    def test_matches_task_and_modalities(self) -> None:
        p = _chat_profile()
        contract = CapabilityContract(task=TaskType.CHAT, inputs=frozenset({Modality.TEXT}))
        assert contract.matches(p)

    def test_mismatched_task_fails(self) -> None:
        p = _chat_profile()
        contract = CapabilityContract(task=TaskType.VISION)
        assert not contract.matches(p)

    def test_missing_input_modality_fails(self) -> None:
        p = _chat_profile()
        contract = CapabilityContract(task=TaskType.CHAT, inputs=frozenset({Modality.IMAGE}))
        assert not contract.matches(p)


class TestRegistry:
    def setup_method(self) -> None:
        self.registry = CapabilityRegistry()

    def test_register_get_unregister(self) -> None:
        p = _chat_profile()
        self.registry.register(p)
        assert self.registry.get(p.id) is p
        assert self.registry.get_invoker(p.id) is None
        self.registry.unregister(p.id)
        assert self.registry.get(p.id) is None

    def test_register_with_invoker(self) -> None:
        p = _chat_profile()
        self.registry.register(p, invoker=object())
        assert self.registry.get_invoker(p.id) is not None

    def test_register_duplicate_raises_without_replace(self) -> None:
        self.registry.register(_chat_profile())
        with pytest.raises(ValueError, match="already registered"):
            self.registry.register(_chat_profile(), replace=False)

    def test_query_by_task(self) -> None:
        self.registry.register(_chat_profile(profile_id="p1"))
        self.registry.register(
            CapabilityProfile(
                id="p2",
                provider="test",
                backend_class=BackendClass.TEXT_LLM,
                tasks=frozenset({TaskType.EMBEDDING}),
            )
        )
        result = self.registry.query(task=TaskType.CHAT)
        assert [p.id for p in result] == ["p1"]

    def test_query_by_provider_and_backend_class(self) -> None:
        self.registry.register(_chat_profile(profile_id="a", provider="p1"))
        self.registry.register(_chat_profile(profile_id="b", provider="p2"))
        assert [p.id for p in self.registry.query(provider="p2")] == ["b"]
        assert self.registry.query(backend_class=BackendClass.TEXT_LLM) is not None

    def test_query_available_only(self) -> None:
        self.registry.register(_chat_profile(profile_id="ok"))
        self.registry.register(_chat_profile(profile_id="no", availability=lambda: False))
        assert [p.id for p in self.registry.query(available_only=True)] == ["ok"]

    def test_query_by_contract(self) -> None:
        self.registry.register(_chat_profile(profile_id="chat"))
        contract = CapabilityContract(task=TaskType.CHAT)
        assert [p.id for p in self.registry.query(contract=contract)] == ["chat"]

    def test_providers_and_clear(self) -> None:
        self.registry.register(_chat_profile(profile_id="a", provider="p1"))
        self.registry.register(_chat_profile(profile_id="b", provider="p2"))
        self.registry.register(_chat_profile(profile_id="c", provider="p2"))
        assert sorted(self.registry.providers()) == ["p1", "p2"]
        self.registry.clear()
        assert self.registry.all() == []


class TestSingletonRegistry:
    def setup_method(self) -> None:
        reset_capability_registry()

    def teardown_method(self) -> None:
        reset_capability_registry()

    def test_global_singleton_identity(self) -> None:
        assert get_capability_registry() is get_capability_registry()

    def test_reset_creates_fresh_registry(self) -> None:
        r1 = get_capability_registry()
        reset_capability_registry()
        assert get_capability_registry() is not r1


class TestProviderStats:
    def setup_method(self) -> None:
        reset_provider_stats()
        self.store = ProviderStatsStore()

    def test_neutral_reliability_without_data(self) -> None:
        assert self.store.reliability("chat", "unknown") == NEUTRAL_SCORE

    def test_record_attempt_success_and_failure(self) -> None:
        self.store.record_attempt("chat", "deepseek", success=True, latency_ms=100)
        self.store.record_attempt("chat", "deepseek", success=True, latency_ms=200)
        self.store.record_attempt("chat", "deepseek", success=False)
        stat = self.store.get("chat", "deepseek")
        assert stat is not None
        assert stat.successes == 2
        assert stat.failures == 1
        assert stat.avg_latency_ms == 150.0

    def test_reliability_is_success_rate_without_quality(self) -> None:
        self.store.record_attempt("chat", "p", success=True)
        self.store.record_attempt("chat", "p", success=False)
        assert self.store.reliability("chat", "p") == 0.5

    def test_quality_ema_blends_reliability(self) -> None:
        self.store.record_attempt("chat", "p", success=True)
        self.store.record_quality("chat", "p", 1.0)
        assert self.store.reliability("chat", "p") > 0.9

    def test_snapshot_and_clear(self) -> None:
        self.store.record_attempt("chat", "p", success=True)
        snap = self.store.snapshot()
        assert "chat:p" in snap
        self.store.clear()
        assert self.store.snapshot() == {}

    def test_global_singleton(self) -> None:
        assert get_provider_stats() is get_provider_stats()


class TestRouter:
    def setup_method(self) -> None:
        self.registry = CapabilityRegistry()

    def test_cost_tier_ascending(self) -> None:
        self.registry.register(_chat_profile(profile_id="paid", cost_tier=2))
        self.registry.register(_chat_profile(profile_id="free", cost_tier=0))
        self.registry.register(_chat_profile(profile_id="mid", cost_tier=1))
        router = CapabilityRouter(self.registry)
        ids = [p.id for p in router.candidates(CapabilityContract(task=TaskType.CHAT))]
        assert ids == ["free", "mid", "paid"]

    def test_preferred_provider_pinned(self) -> None:
        self.registry.register(_chat_profile(profile_id="a", provider="x"))
        self.registry.register(_chat_profile(profile_id="b", provider="y"))
        router = CapabilityRouter(self.registry)
        cands = router.candidates(CapabilityContract(task=TaskType.CHAT), preferred_provider="y")
        assert [p.id for p in cands] == ["b"]

    def test_offline_floor_appended_last(self) -> None:
        self.registry.register(_chat_profile(profile_id="real", cost_tier=2))
        self.registry.register(
            CapabilityProfile(
                id="offline",
                provider="mock",
                backend_class=BackendClass.OFFLINE,
                tasks=frozenset({TaskType.CHAT}),
                cost_tier=0,
            )
        )
        router = CapabilityRouter(self.registry)
        ids = [p.id for p in router.candidates(CapabilityContract(task=TaskType.CHAT))]
        assert ids == ["real", "offline"]

    def test_available_only_filters_unavailable(self) -> None:
        self.registry.register(_chat_profile(profile_id="ok"))
        self.registry.register(_chat_profile(profile_id="no", availability=lambda: False))
        router = CapabilityRouter(self.registry)
        cands = router.candidates(CapabilityContract(task=TaskType.CHAT), available_only=True)
        assert [p.id for p in cands] == ["ok"]

    def test_select_returns_best_or_none(self) -> None:
        self.registry.register(_chat_profile(profile_id="a", cost_tier=0))
        router = CapabilityRouter(self.registry)
        assert router.select(CapabilityContract(task=TaskType.CHAT)).id == "a"
        assert router.select(CapabilityContract(task=TaskType.EMBEDDING)) is None

    def test_provider_options_distinct(self) -> None:
        self.registry.register(_chat_profile(profile_id="a", provider="x"))
        self.registry.register(_chat_profile(profile_id="b", provider="x"))
        self.registry.register(_chat_profile(profile_id="c", provider="y"))
        router = CapabilityRouter(self.registry)
        assert router.provider_options(CapabilityContract(task=TaskType.CHAT)) == ["x", "y"]


class TestAdapters:
    def test_chat_model_text_llm(self) -> None:
        spec = BUILTIN_MODELS["deepseek-v4-flash"]
        profile = from_model_spec(spec)
        assert profile.provider == "deepseek"
        assert profile.backend_class == BackendClass.TEXT_LLM
        assert profile.supports_task(TaskType.CHAT)
        assert not profile.supports_task(TaskType.VISION)
        assert profile.supports_streaming is True
        assert profile.requires_credentials is True

    def test_vision_model_multimodal(self) -> None:
        spec = ModelSpec(
            name="gemini-2.0-flash",
            provider="gemini",
            context_window=128000,
            supports_vision=True,
        )
        profile = from_model_spec(spec)
        assert profile.backend_class == BackendClass.MULTIMODAL_LLM
        assert profile.supports_task(TaskType.VISION)
        assert profile.supports_input("image")

    def test_mock_is_offline(self) -> None:
        profile = from_model_spec(BUILTIN_MODELS["mock"])
        assert profile.backend_class == BackendClass.OFFLINE
        assert profile.cost_tier == 0
        assert profile.requires_credentials is False


class TestBootstrap:
    def test_bootstrap_from_catalog(self) -> None:
        registry = CapabilityRegistry()
        catalog = ModelCatalog()
        registered = bootstrap_capabilities(catalog, registry=registry)
        assert len(registered) == len(catalog.list_models())
        assert registry.get("chat:deepseek:deepseek-v4-flash") is not None

    def test_bootstrap_default_registry(self) -> None:
        reset_capability_registry()
        try:
            registered = bootstrap_capabilities(ModelCatalog())
            assert registered
        finally:
            reset_capability_registry()

    def test_bootstrap_skips_empty_catalog(self) -> None:
        registry = CapabilityRegistry()
        registered = bootstrap_capabilities(None, registry=registry)
        assert registered == []


def test_llm_config_provider_registry_still_works() -> None:
    cfg = BandiConfig()
    # 默认模型 = deepseek-v4.1-flash（网关命名；官方现役名 deepseek-flash 亦在目录内）
    assert cfg.llm.default_model == "deepseek-v4.1-flash"
