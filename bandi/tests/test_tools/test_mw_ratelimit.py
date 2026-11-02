from __future__ import annotations

import time

import pytest

from bandi.core.message import ToolCall
from bandi.tools.middleware.ratelimit_mw import RateLimitMiddleware, SlidingWindowRateLimiter

pytestmark = pytest.mark.asyncio


async def _fake_executor(ctx, call):
    from bandi.core.message import ToolResult

    return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


class TestSlidingWindowRateLimiter:
    async def test_under_limit_allowed(self):
        limiter = SlidingWindowRateLimiter(max_calls=5, window_sec=60)
        allowed, _ = await limiter.check("test_key")
        assert allowed is True

    async def test_over_limit_blocked(self):
        limiter = SlidingWindowRateLimiter(max_calls=2, window_sec=60)
        assert (await limiter.check("test_key"))[0] is True
        assert (await limiter.check("test_key"))[0] is True
        allowed, retry_after = await limiter.check("test_key")
        assert allowed is False
        assert retry_after > 0

    async def test_window_expires_old_entries(self):
        limiter = SlidingWindowRateLimiter(max_calls=2, window_sec=0.1)
        assert (await limiter.check("test_key"))[0] is True
        assert (await limiter.check("test_key"))[0] is True
        assert (await limiter.check("test_key"))[0] is False
        time.sleep(0.15)
        allowed, _ = await limiter.check("test_key")
        assert allowed is True


class TestRateLimitMiddleware:
    async def test_under_limit(self):
        limiter = SlidingWindowRateLimiter(max_calls=5, window_sec=60)
        mw = RateLimitMiddleware(limiter)
        from bandi.tools.pipeline import ToolContext

        ctx = ToolContext(user_id="user1")
        call = ToolCall(id="1", name="read", arguments={})
        result = await mw.before_execute(ctx, call)
        assert result.allow is True

    async def test_over_limit(self):
        limiter = SlidingWindowRateLimiter(max_calls=1, window_sec=60)
        mw = RateLimitMiddleware(limiter)
        from bandi.tools.pipeline import ToolContext

        ctx = ToolContext(user_id="user1")
        call = ToolCall(id="1", name="read", arguments={})
        await mw.before_execute(ctx, call)
        result = await mw.before_execute(ctx, call)
        assert result.allow is False
        assert "Rate limited" in result.reason
