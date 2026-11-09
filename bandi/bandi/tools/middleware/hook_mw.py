# 功能：hooks 中间件——PreToolUse 决策/参数改写 + PostToolUse 上下文注入。
# 装配顺序：HookBefore 在最前（deny 短路 + updatedInput 改写），HookAfter 在最末
# （after 链同序执行，结果追加 [hook context]）；hook allow 不绕过后续权限链。
from __future__ import annotations

from typing import Any

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.hooks import (
    HOOK_POST_TOOL_USE,
    HOOK_POST_TOOL_USE_FAILURE,
    HOOK_PRE_TOOL_USE,
    HookRunner,
)
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware


def _tool_payload(
    ctx: ToolContext, call: ToolCall, result: ToolResult | None = None
) -> dict[str, Any]:
    # 功能：hook 输入 payload（工具名/参数/结果摘要）
    payload: dict[str, Any] = {
        "tool_name": call.name,
        "tool_call_id": call.id,
        "arguments": dict(call.arguments or {}),
        "session_id": str((ctx.extra or {}).get("session_id") or ctx.agent_id or ""),
    }
    if result is not None:
        payload["success"] = result.success
        payload["output"] = (result.output or "")[:4000]
        payload["error"] = (result.error or "")[:1000]
    return payload


class HookBeforeMiddleware(ToolMiddleware):
    def __init__(self, runner: HookRunner | None = None) -> None:
        self._runner = runner

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        runner = self._runner
        if runner is None or not runner.has_rules:
            return MiddlewareResult(allow=True)
        outcome = await runner.run_event(HOOK_PRE_TOOL_USE, _tool_payload(ctx, call))
        if outcome.decision == "deny":
            return MiddlewareResult(
                allow=False,
                reason=f"denied by hook: {outcome.reason or call.name}",
            )
        if outcome.updated_input is not None:
            return MiddlewareResult(allow=True, modified_params=outcome.updated_input)
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        return result


class HookAfterMiddleware(ToolMiddleware):
    def __init__(self, runner: HookRunner | None = None) -> None:
        self._runner = runner

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        runner = self._runner
        if runner is None or not runner.has_rules:
            return result
        event = HOOK_POST_TOOL_USE if result.success else HOOK_POST_TOOL_USE_FAILURE
        outcome = await runner.run_event(event, _tool_payload(ctx, call, result))
        if outcome.additional_context:
            base = result.output or ""
            result.output = f"{base}\n[hook context] {outcome.additional_context}"
        return result
