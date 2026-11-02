# 功能：遥测中间件（TelemetryMiddleware 工具执行耗时计量）
from __future__ import annotations

import time

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware

_MS_PER_SEC = 1000


class TelemetryMiddleware(ToolMiddleware):
    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        ctx.start_time = time.monotonic()
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        elapsed = (time.monotonic() - ctx.start_time) * _MS_PER_SEC
        result.duration_ms = elapsed
        return result
