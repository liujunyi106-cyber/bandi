from __future__ import annotations

import pytest

from bandi.llm.strategy import ModelFallback, ProviderChainExhaustedError


class _MockRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, object] = {}

    def add_provider(self, model: str, provider: object) -> None:
        self._providers[model] = provider

    def get_provider(self, model: str, config: object) -> object:
        p = self._providers.get(model)
        if p is None:
            raise ValueError(f"No provider for {model}")
        return p


class _MockConfig:
    class _LLM:
        timeout = 10

    llm = _LLM()


class _OkProvider:
    async def complete(self, messages, **kwargs):
        return "ok"


class _FailingProvider:
    async def complete(self, messages, **kwargs):
        raise RuntimeError("down")


class _SlowProvider:
    async def complete(self, messages, **kwargs):
        import asyncio

        await asyncio.sleep(10)
        return "slow"


class TestModelFallback:
    @pytest.mark.asyncio
    async def test_first_model_succeeds(self):
        reg = _MockRegistry()
        reg.add_provider("primary", _OkProvider())
        fb = ModelFallback(fallback_chain=["backup"])
        result, model = await fb.complete_with_fallback("primary", [], reg, _MockConfig())
        assert result == "ok"
        assert model == "primary"

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self):
        reg = _MockRegistry()
        reg.add_provider("primary", _FailingProvider())
        reg.add_provider("backup", _OkProvider())
        fb = ModelFallback(fallback_chain=["backup"])
        result, model = await fb.complete_with_fallback("primary", [], reg, _MockConfig())
        assert result == "ok"
        assert model == "backup"

    @pytest.mark.asyncio
    async def test_all_exhausted_raises(self):
        reg = _MockRegistry()
        reg.add_provider("primary", _FailingProvider())
        reg.add_provider("backup", _FailingProvider())
        fb = ModelFallback(fallback_chain=["backup"])
        with pytest.raises(ProviderChainExhaustedError):
            await fb.complete_with_fallback("primary", [], reg, _MockConfig())

    def test_get_fallback_chain(self):
        fb = ModelFallback(fallback_chain=["a", "b"])
        assert fb.get_fallback_chain() == ["a", "b"]

    def test_default_fallback_chain(self):
        fb = ModelFallback()
        assert fb.get_fallback_chain() == []

    @pytest.mark.asyncio
    async def test_preferred_not_duplicated_in_chain(self):
        reg = _MockRegistry()
        reg.add_provider("primary", _FailingProvider())
        reg.add_provider("backup", _OkProvider())
        fb = ModelFallback(fallback_chain=["primary", "backup"])
        result, model = await fb.complete_with_fallback("primary", [], reg, _MockConfig())
        assert model == "backup"
