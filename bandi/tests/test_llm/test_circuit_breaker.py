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
    msg = "call failed"
    raise ConnectionError(msg)


def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_allows_successful_calls():
    cb = CircuitBreaker(failure_threshold=3)
    result = await cb.call(_ok)
    assert result == "ok"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call(_fail)
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_rejects_when_open():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    with pytest.raises(ConnectionError):
        await cb.call(_fail)
    with pytest.raises(CircuitBreakerOpenError):
        await cb.call(_ok)


@pytest.mark.asyncio
async def test_circuit_breaker_transitions_to_half_open_after_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.5, half_open_max_retries=2)
    with pytest.raises(ConnectionError):
        await cb.call(_fail)
    await asyncio.sleep(0.6)
    with pytest.raises(ConnectionError):
        await cb.call(_fail)
    assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_after_half_open_success():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.5)
    with pytest.raises(ConnectionError):
        await cb.call(_fail)
    await asyncio.sleep(0.6)
    result = await cb.call(_ok)
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_retry_policy_succeeds_on_first_try():
    rp = RetryPolicy(max_retries=3)
    result = await rp.execute(_ok)
    assert result == "ok"


@pytest.mark.asyncio
async def test_retry_policy_retries_on_failure():
    rp = RetryPolicy(max_retries=3, base_delay=0.01, backoff_factor=1.0, jitter=False)
    call_count = 0

    async def fail_twice():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            msg = "fail"
            raise ConnectionError(msg)
        return "ok"

    result = await rp.execute(fail_twice)
    assert result == "ok"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_policy_exhausts_retries():
    rp = RetryPolicy(max_retries=2, base_delay=0.01, backoff_factor=1.0, jitter=False)
    with pytest.raises(ConnectionError):
        await rp.execute(_fail)


@pytest.mark.asyncio
async def test_retry_policy_with_circuit_breaker():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
    rp = RetryPolicy(max_retries=2, base_delay=0.01, backoff_factor=1.0, jitter=False)
    with pytest.raises(CircuitBreakerOpenError):
        await rp.execute(_fail, circuit_breaker=cb)


def test_exponential_backoff_delay():
    rp = RetryPolicy(base_delay=1.0, backoff_factor=2.0, max_delay=30.0, jitter=False)
    assert rp._compute_delay(0) == 1.0
    assert rp._compute_delay(1) == 2.0
    assert rp._compute_delay(2) == 4.0
    assert rp._compute_delay(3) == 8.0


def test_exponential_backoff_with_jitter():
    rp = RetryPolicy(base_delay=1.0, backoff_factor=2.0, max_delay=30.0, jitter=True)
    delays = [rp._compute_delay(2) for _ in range(100)]
    assert all(2.0 <= d <= 4.0 for d in delays)


@pytest.mark.asyncio
async def test_retry_policy_bubbles_non_retriable_errors():
    rp = RetryPolicy(max_retries=2, base_delay=0.01)

    async def raises_value_error():
        msg = "invalid"
        raise ValueError(msg)

    with pytest.raises(ValueError):
        await rp.execute(raises_value_error)
