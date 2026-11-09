# 功能：权限中间件（PermissionMiddleware 工具执行前的权限验证）
from __future__ import annotations

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.base import BaseTool
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware


class PermissionMiddleware(ToolMiddleware):
    def __init__(self, pipeline: PermissionsPipeline | None = None) -> None:
        self._pipeline = pipeline

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        pp = self._pipeline or ctx.extra.get("permissions_pipeline")
        if pp is None:
            return MiddlewareResult(allow=True)
        tool: BaseTool | None = ctx.extra.get("tool")
        verdict = await pp.check(
            call,
            tool,
            ctx.mode,
            agent_id=ctx.agent_id,
            role=ctx.role,
            scope=ctx.scope,
        )
        if not verdict.allow:
            return MiddlewareResult(allow=False, reason=verdict.reason)
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        return result
