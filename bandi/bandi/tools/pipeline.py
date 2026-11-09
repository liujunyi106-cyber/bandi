# 功能：工具管道（ToolPipeline 中间件链 + 上下文/结果传递）
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bandi.core.message import ToolCall, ToolResult


@dataclass
class ToolContext:
    user_id: str = "default"
    agent_id: str = "default"
    role: str = "user"
    scope: str = "default"
    mode: str = "execute"
    start_time: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)
    _retrying: bool = False
    _run_tool: Callable | None = None


@dataclass
class MiddlewareResult:
    allow: bool = True
    reason: str | None = None
    modified_params: dict[str, Any] | None = None


class ToolMiddleware(ABC):
    @abstractmethod
    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult: ...

    @abstractmethod
    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult: ...


class ToolPipeline:
    def __init__(self) -> None:
        self._middlewares: list[ToolMiddleware] = []

    def add(self, mw: ToolMiddleware, index: int | None = None) -> None:
        if index is not None:
            self._middlewares.insert(index, mw)
        else:
            self._middlewares.append(mw)

    async def execute(
        self,
        ctx: ToolContext,
        call: ToolCall,
        run_tool: Callable,
    ) -> ToolResult:
        ctx._run_tool = run_tool
        ctx._pipeline_execute = self.retry
        before = await self._run_before(ctx, call)
        if before is not None:
            return before
        return await self._run_execute(ctx, call)

    async def _run_before(self, ctx: ToolContext, call: ToolCall) -> ToolResult | None:
        """执行 before 链；返回非 None 表示被拦截（短路）。"""
        ctx.start_time = time.monotonic()
        for mw in self._middlewares:
            br = await mw.before_execute(ctx, call)
            if not br.allow:
                return ToolResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    success=False,
                    output="",
                    error=br.reason or "blocked by middleware",
                    # TOOL_ERROR 事件（拒绝可观测；原 deny 结果静默返回）
                    meta={"middleware_blocked": True},
                )
            if br.modified_params:
                call.arguments = br.modified_params
        return None

    async def _run_execute(self, ctx: ToolContext, call: ToolCall) -> ToolResult:
        """执行工具 + after 链（重试只重跑本段，不重复 before 副作用）。"""
        if ctx._run_tool is None:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error="run_tool not bound to context",
            )
        result = await ctx._run_tool(ctx, call)
        for mw in self._middlewares:
            result = await mw.after_execute(ctx, call, result)
        return result

    async def retry(self, ctx: ToolContext, call: ToolCall) -> ToolResult:
        return await self._run_execute(ctx, call)
