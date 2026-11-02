import pytest

from bandi.llm.strategy import FallbackEngine, ProviderChainExhaustedError


class TestStreamReconnect:
    @pytest.mark.asyncio
    async def test_retry_on_connection_error(self) -> None:
        attempts = 0

        async def stream_impl(messages, config):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("stream disconnected")
            return "connected"

        engine = FallbackEngine(fallback_chain=["primary", "backup"])
        result = await engine.call_with_fallback([], {"provider": "primary"}, stream_impl)
        assert result == "connected"
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_retry_exhaustion_fails(self) -> None:
        async def stream_impl(messages, config):
            raise ConnectionResetError("always fail")

        engine = FallbackEngine(fallback_chain=["primary"])
        with pytest.raises(ProviderChainExhaustedError):
            await engine.call_with_fallback([], {"provider": "primary"}, stream_impl)
            await engine.call_with_fallback([], {"provider": "primary"}, stream_impl)
