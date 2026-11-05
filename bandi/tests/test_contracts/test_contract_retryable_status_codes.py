"""契约：限流与请求超时应重试，不可重试的 4xx 才直接抛。

缺陷：`RetryPolicy.execute` 把 `400 <= status < 500` 一律判为不可重试，把
**429 Too Many Requests** 和 **408 Request Timeout** 也算进去——两者恰是
「等一会就好」的瞬时故障。

后果（真机）：L1 `L1-G09-01` 中 verifier judge 撞 429 后直接失败，fail-closed
把一条**本来正确**的跨轮记忆召回（「项目代号青龙 / 端口 7421」）判成 ESCALATED。
"""

from __future__ import annotations

import httpx
import pytest

from bandi.llm.circuit_breaker import RetryPolicy


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"status {code}", request=request, response=httpx.Response(code, request=request)
    )


class TestRetryableStatuses:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [429, 408, 409])
    async def test_retried(self, code: int) -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _status_error(code)
            return "ok"

        policy = RetryPolicy(max_retries=3, base_delay=0.001, jitter=False)
        assert await policy.execute(flaky) == "ok"
        assert calls["n"] == 3, f"{code} 应重试而非首错即抛"


class TestNonRetryableStatuses:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    async def test_not_retried(self, code: int) -> None:
        """破坏者视角：请求本身错了，重试只是放大错误与耗时。"""
        calls = {"n": 0}

        async def bad() -> str:
            calls["n"] += 1
            raise _status_error(code)

        policy = RetryPolicy(max_retries=3, base_delay=0.001, jitter=False)
        with pytest.raises(httpx.HTTPStatusError):
            await policy.execute(bad)
        assert calls["n"] == 1, f"{code} 不该重试（实调 {calls['n']} 次）"


class TestServerErrorsUnaffected:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [500, 502, 503])
    async def test_5xx_still_retried(self, code: int) -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise _status_error(code)
            return "ok"

        policy = RetryPolicy(max_retries=2, base_delay=0.001, jitter=False)
        assert await policy.execute(flaky) == "ok"
        assert calls["n"] == 2


class TestBudgetExhaustion:
    @pytest.mark.asyncio
    async def test_retry_budget_is_respected(self) -> None:
        """429 重试也不能无限——预算耗尽后如实抛错。"""
        calls = {"n": 0}

        async def always_limited() -> str:
            calls["n"] += 1
            raise _status_error(429)

        policy = RetryPolicy(max_retries=2, base_delay=0.001, jitter=False)
        with pytest.raises(httpx.HTTPStatusError):
            await policy.execute(always_limited)
        assert calls["n"] == 3, f"1 次首发 + 2 次重试 = 3 次，实调 {calls['n']}"
