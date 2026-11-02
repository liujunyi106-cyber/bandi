import pytest

from bandi.llm.strategy import FallbackEngine, ProviderChainExhaustedError


class TestFallbackEngine:
    @pytest.mark.asyncio
    async def test_first_provider_succeeds(self) -> None:
        engine = FallbackEngine(fallback_chain=["primary", "backup"])

        async def call(messages, config):
            return "ok"

        result = await engine.call_with_fallback([], {"provider": "primary"}, call)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self) -> None:
        calls = []

        async def call(messages, config):
            calls.append(config.get("provider"))
            if config.get("provider") == "primary":
                raise ConnectionError("down")
            return "ok"

        engine = FallbackEngine(fallback_chain=["primary", "backup"])
        result = await engine.call_with_fallback([], {"provider": "primary"}, call)
        assert result == "ok"
        assert calls == ["primary", "backup"]

    @pytest.mark.asyncio
    async def test_all_exhausted_raises(self) -> None:
        async def call(messages, config):
            raise ConnectionError("always fail")

        engine = FallbackEngine(fallback_chain=["a", "b"])
        with pytest.raises(ProviderChainExhaustedError):
            await engine.call_with_fallback([], {"provider": "a"}, call)
