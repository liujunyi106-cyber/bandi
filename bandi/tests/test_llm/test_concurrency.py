# 功能：熔断器 + 重试策略 并发测试 — 异步并发调用

import asyncio

import pytest

from bandi.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    RetryPolicy,
)


async def _ok():
    return "ok"


async def _fail():
    msg = "fail"
    raise ConnectionError(msg)


class TestCircuitBreakerConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_calls_no_race(self) -> None:
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        results = await asyncio.gather(*[cb.call(_ok) for _ in range(20)], return_exceptions=True)
        successes = [r for r in results if r == "ok"]
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 20
        assert len(errors) == 0
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_concurrent_failures_trigger_open_once(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        results = await asyncio.gather(*[cb.call(_fail) for _ in range(10)], return_exceptions=True)
        circuit_opens = [r for r in results if isinstance(r, CircuitBreakerOpenError)]
        failures = [r for r in results if isinstance(r, ConnectionError)]
        assert len(failures) >= 3
        assert len(circuit_opens) >= 1
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_concurrent_call_with_retry_policy(self) -> None:
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        rp = RetryPolicy(max_retries=1, base_delay=0.01, backoff_factor=1.0, jitter=False)

        async def fail_then_ok():
            call_count = 0

            async def inner():
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    msg = "fail"
                    raise ConnectionError(msg)
                return "ok"

            return await rp.execute(inner, circuit_breaker=cb)

        results = await asyncio.gather(*[fail_then_ok() for _ in range(10)], return_exceptions=True)
        circuit_opens = [r for r in results if isinstance(r, CircuitBreakerOpenError)]
        successes = [r for r in results if r == "ok"]
        assert len(successes) + len(circuit_opens) == 10
