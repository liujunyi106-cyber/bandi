from __future__ import annotations

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse
from bandi.llm.capabilities import (
    BackendClass,
    CapabilityProfile,
    CapabilityRegistry,
    TaskType,
    get_provider_stats,
    reset_provider_stats,
)
from bandi.llm.mock import MockProvider
from bandi.llm.registry import ProviderRegistry
from bandi.llm.strategy import (
    CapabilityFallback,
    ComplexityEstimator,
    ModelRouter,
    ProviderChainExhaustedError,
)


class TestComplexityEstimator:
    def setup_method(self) -> None:
        self.estimator = ComplexityEstimator()

    def test_simple_low_tokens_no_tools(self) -> None:
        assert self.estimator.estimate(100, 0) == "simple"

    def test_simple_low_tokens_few_tools(self) -> None:
        assert self.estimator.estimate(500, 2) == "simple"

    def test_moderate_medium_tokens(self) -> None:
        assert self.estimator.estimate(2000, 2) == "moderate"

    def test_moderate_many_tools(self) -> None:
        assert self.estimator.estimate(500, 4) == "moderate"

    def test_complex_high_tokens(self) -> None:
        assert self.estimator.estimate(5000, 0) == "complex"

    def test_complex_many_tools(self) -> None:
        assert self.estimator.estimate(500, 6) == "complex"

    def test_complex_high_tokens_and_tools(self) -> None:
        assert self.estimator.estimate(10000, 10) == "complex"


class TestModelRouter:
    def setup_method(self) -> None:
        self.router = ModelRouter()

    def test_select_pool_simple(self) -> None:
        pool = self.router.select_pool(100, 0)
        assert "deepseek-v4.1-flash" in pool, "池首须为 V4.1-Flash 网关命名"
        assert "deepseek-flash" in pool, "官方命名须并列在池中（双命名端点兜底）"

    def test_select_pool_complex(self) -> None:
        pool = self.router.select_pool(10000, 10)
        assert "gpt-5.2" in pool

    def test_select_best_simple(self) -> None:
        best = self.router.select_best(100, 0)
        assert best == "deepseek-v4.1-flash"

    def test_select_best_complex(self) -> None:
        best = self.router.select_best(10000, 10)
        assert best == "gpt-5.2"

    def test_pools_all_registered_in_catalog(self) -> None:
        """
        gemini-2.0-flash/gemini-2.0-pro（目录不存在），每轮 fallback 必产生
        1-2 次必失败的无效尝试（Day 122 接线后影响放大）。"""
        from bandi.llm.model_catalog import BUILTIN_MODELS

        for pool in (
            self.router.select_pool(100, 0),
            self.router.select_pool(2000, 4),
            self.router.select_pool(10000, 10),
        ):
            for model in pool:
                assert model in BUILTIN_MODELS, (
                    f"候选池含未注册模型 {model}（BUILTIN_MODELS 缺失）——每轮 fallback 必失败"
                )

    def test_pools_cover_chinese_model_line(self) -> None:
        from bandi.llm.model_catalog import BUILTIN_MODELS

        for name in ("kimi-k3", "glm-5.3", "glm-5.2"):
            assert name in BUILTIN_MODELS, f"国产模型 {name} 必须注册进 BUILTIN_MODELS"


class TestModelRouterStreamFallback:
    def setup_method(self) -> None:
        self.router = ModelRouter()

    @pytest.mark.asyncio
    async def test_stream_fallback_on_iteration_error(self) -> None:
        class _FailFirstProvider(MockProvider):
            async def stream(self, messages, tools=None, **kwargs):
                yield None  # 占位以触发迭代
                raise RuntimeError("boom after first yield")

        class _OkProvider(MockProvider):
            async def stream(self, messages, tools=None, **kwargs):
                yield "ok-event"

        registry = ProviderRegistry()
        registry.register("p1", _FailFirstProvider)
        registry.register("p2", _OkProvider)
        registry.register_model("preferred", "p1")
        registry.register_model("deepseek-v4.1-flash", "p2")
        registry.register_model("deepseek-flash", "p2")
        cfg = BandiConfig()

        stream, used = await self.router.stream_with_fallback(
            "preferred",
            [{"role": "user", "content": "hi"}],
            registry,
            cfg,
        )
        events = [e async for e in stream]
        assert used == "preferred"
        assert events[-1] == "ok-event", "迭代期错误必须降级到备用模型并产出其事件"
        assert len(events) == 2, "首模型已产出事件保留 + 备用模型产出"

    @pytest.mark.asyncio
    async def test_stream_all_fail_raises_exhausted(self) -> None:
        class _FailProvider(MockProvider):
            async def stream(self, messages, tools=None, **kwargs):
                yield None
                raise RuntimeError("boom")

        registry = ProviderRegistry()
        registry.register("p1", _FailProvider)
        registry.register("p2", _FailProvider)
        registry.register_model("preferred", "p1")
        registry.register_model("deepseek-v4.1-flash", "p2")
        registry.register_model("deepseek-flash", "p2")
        cfg = BandiConfig()

        stream, _ = await self.router.stream_with_fallback(
            "preferred",
            [{"role": "user", "content": "hi"}],
            registry,
            cfg,
        )
        with pytest.raises(ProviderChainExhaustedError):
            async for _ in stream:
                pass


class TestCapabilityFallback:
    def setup_method(self) -> None:
        reset_provider_stats()
        self.cap_registry = CapabilityRegistry()
        self._add_cap("preferred", "p1", cost_tier=2)
        self._add_cap("alt", "p2", cost_tier=1)
        self.provider_registry = ProviderRegistry()
        self.provider_registry.register("p1", MockProvider)
        self.provider_registry.register("p2", MockProvider)
        self.provider_registry.register_model("preferred", "p1")
        self.provider_registry.register_model("alt", "p2")
        self.config = BandiConfig()
        self.fallback = CapabilityFallback(self.cap_registry, self.config)

    def teardown_method(self) -> None:
        reset_provider_stats()

    def _add_cap(self, model: str, provider: str, cost_tier: int) -> None:
        self.cap_registry.register(
            CapabilityProfile(
                id=f"chat:{provider}:{model}",
                provider=provider,
                backend_class=BackendClass.TEXT_LLM,
                tasks=frozenset({TaskType.CHAT}),
                default_model=model,
                cost_tier=cost_tier,
            )
        )

    @pytest.mark.asyncio
    async def test_success_on_preferred(self) -> None:
        p = self.provider_registry.get_provider("preferred", self.config)
        p.add_response(LLMResponse(content="ok"))
        response, used = await self.fallback.complete_with_fallback(
            "preferred",
            [{"role": "user", "content": "hi"}],
            self.provider_registry,
            self.config,
        )
        assert response.content == "ok"
        assert used == "preferred"

    @pytest.mark.asyncio
    async def test_preferred_fails_raises_without_candidates(self) -> None:
        """候选链已删除（不搞候选）——preferred 失败即显式抛，
        不再自动切换其他模型（坏凭证候选曾掩盖主模型错因）。"""
        preferred = self.provider_registry.get_provider("preferred", self.config)
        preferred.add_response(RuntimeError)
        with pytest.raises(ProviderChainExhaustedError):
            await self.fallback.complete_with_fallback(
                "preferred",
                [{"role": "user", "content": "hi"}],
                self.provider_registry,
                self.config,
            )

    @pytest.mark.asyncio
    async def test_all_fail_raises_exhausted(self) -> None:
        for model in ("preferred", "alt"):
            p = self.provider_registry.get_provider(model, self.config)
            p.add_response(RuntimeError)
        with pytest.raises(ProviderChainExhaustedError):
            await self.fallback.complete_with_fallback(
                "preferred",
                [{"role": "user", "content": "hi"}],
                self.provider_registry,
                self.config,
            )

    @pytest.mark.asyncio
    async def test_stats_recorded_for_preferred_only(self) -> None:
        """候选链已删：只记录 preferred 的失败——不再有第二候选的 success 记录。"""
        preferred = self.provider_registry.get_provider("preferred", self.config)
        preferred.add_response(RuntimeError)
        try:
            await self.fallback.complete_with_fallback(
                "preferred",
                [{"role": "user", "content": "hi"}],
                self.provider_registry,
                self.config,
            )
        except ProviderChainExhaustedError:
            pass
        stats = get_provider_stats()
        assert stats.get("chat", "p1") is not None
        assert stats.get("chat", "p1").failures == 1
        alt_stats = stats.get("chat", "p2")
        assert alt_stats is None or alt_stats.successes == 0, "候选已删——不得有 alt 成功记录"

    def test_candidate_models_single_preferred(self) -> None:
        """候选链已删：_candidate_models 恒返回单候选
        [preferred]——复杂度池/能力池扩展通道整删（防坏凭证候选掩盖主模型错因）。"""
        messages = [{"role": "user", "content": "x" * 20000}]
        candidates = self.fallback._candidate_models("preferred", messages)
        assert candidates == ["preferred"]
