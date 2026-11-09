# 功能：限流中间件（滑动窗口速率限制）
from __future__ import annotations

import time
from collections import deque

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int = 30, window_sec: int = 60, max_keys: int = 4096) -> None:
        self._max_calls = max_calls
        self._window = window_sec
        # 空闲 key 回收上限（防未认证前缀/异常来源让字典无界增长）
        self._max_keys = max(1, int(max_keys))
        self._calls: dict[str, deque[float]] = {}

    async def check(self, key: str, tokens: int = 1) -> tuple[bool, float]:
        now = time.monotonic()
        if key not in self._calls:
            if len(self._calls) >= self._max_keys:
                self._prune_idle(now)
            self._calls[key] = deque()

        q = self._calls[key]
        while q and q[0] < now - self._window:
            q.popleft()

        if len(q) + tokens > self._max_calls:
            retry_after = q[0] + self._window - now if q else float(self._window)
            return False, retry_after

        q.append(now)
        return True, 0.0

    def _prune_idle(self, now: float) -> None:
        """回收窗口内无活跃记录的 key（低频调用：仅在新 key 触顶时）。"""
        idle = [key for key, q in self._calls.items() if not q or q[-1] < now - self._window]
        for key in idle:
            del self._calls[key]


class RateLimitMiddleware(ToolMiddleware):
    def __init__(self, limiter: SlidingWindowRateLimiter) -> None:
        self._limiter = limiter

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        key = f"{ctx.user_id}:{call.name}"
        allowed, retry_after = await self._limiter.check(key)
        if not allowed:
            return MiddlewareResult(
                allow=False,
                reason=f"Rate limited for {call.name}. Retry after {retry_after:.1f}s",
            )
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        return result
