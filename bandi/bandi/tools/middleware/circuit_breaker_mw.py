# 功能：熔断中间件（工具级熔断状态 + 断路器）
from __future__ import annotations

import time
from enum import Enum

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerMiddleware(ToolMiddleware):
    def __init__(self, threshold: int = 5, recovery_timeout: float = 60.0) -> None:
        self._states: dict[str, CircuitState] = {}
        self._failure_counts: dict[str, int] = {}
        self._last_failure: dict[str, float] = {}
        self._threshold = threshold
        self._recovery_timeout = recovery_timeout

    def _get_state(self, tool_name: str) -> CircuitState:
        return self._states.get(tool_name, CircuitState.CLOSED)

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        state = self._get_state(call.name)
        if state == CircuitState.OPEN:
            last_fail = self._last_failure.get(call.name, 0.0)
            if time.monotonic() - last_fail >= self._recovery_timeout:
                self._states[call.name] = CircuitState.HALF_OPEN
            else:
                return MiddlewareResult(
                    allow=False,
                    reason=f"Circuit breaker open for {call.name}",
                )
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        state = self._get_state(call.name)
        if result.success:
            if state == CircuitState.HALF_OPEN:
                self._states[call.name] = CircuitState.CLOSED
            self._failure_counts[call.name] = 0
            return result
        if ctx._retrying:
            # 重试期间的失败不计入熔断——否则 1 次原始失败 + N 次重试失败
            # 重复计数，阈值被缩小到 1/N（重试成功由 success 分支重置计数）
            return result
        self._failure_counts[call.name] = self._failure_counts.get(call.name, 0) + 1
        self._last_failure[call.name] = time.monotonic()
        if state == CircuitState.HALF_OPEN:
            # 半开期探测失败必须立即回 OPEN（重新计时）——半开期无保护
            # 直通禁止，否则失败命令在每次超时后都被无限重试
            self._states[call.name] = CircuitState.OPEN
        elif self._failure_counts[call.name] >= self._threshold:
            self._states[call.name] = CircuitState.OPEN
        return result
