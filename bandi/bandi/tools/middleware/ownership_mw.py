# 功能：Ownership 守卫中间件 — 工具调用前按 ownership contract 拦截越界写/越权协作

from __future__ import annotations

from typing import Any

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware

_ACQUISITION_TOOLS = {"bash", "python_exec"}


def _acquisition_guard_violation(call: ToolCall, work_item: Any) -> str | None:
    from bandi.company.data_acquisition_policy import (
        ACQUISITION_SHELL_PREFIXES,
        is_projection_scoped_acquisition_shell_command,
        task_projection_id,
        workspace_root_for_task,
    )

    if call.name not in _ACQUISITION_TOOLS:
        return None
    if task_projection_id(work_item) != "data_acquisition":
        return None
    arguments = dict(call.arguments or {})
    command = str(arguments.get("command", "") or "").strip()
    if not command:
        return None
    if str(command.split()[0] or "").lower() not in ACQUISITION_SHELL_PREFIXES:
        return None
    scoped = is_projection_scoped_acquisition_shell_command(
        command=command,
        task=work_item,
        working_directory=str(arguments.get("cwd") or arguments.get("working_directory") or ""),
        target_output_dir=str(workspace_root_for_task(work_item) or ""),
    )
    if not scoped:
        return (
            "Acquisition shell command output escapes the data acquisition projection "
            "workspace. Downloads must write inside the task workspace."
        )
    return None


class OwnershipMiddleware(ToolMiddleware):
    def __init__(self, work_item_provider: callable | None = None) -> None:
        self._work_item_provider = work_item_provider

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        from bandi.company.collaboration_policy import ownership_guard_violation

        work_item = None
        if self._work_item_provider is not None:
            work_item = self._work_item_provider(ctx, call)
        else:
            work_item = ctx.extra.get("work_item")
        if work_item is None:
            return MiddlewareResult(
                allow=False, reason="work_item not available for ownership check"
            )
        violation = ownership_guard_violation(
            work_item=work_item,
            tool_name=call.name,
            arguments=dict(call.arguments or {}),
            org_engine=ctx.extra.get("org_engine"),
        )
        if violation is not None:
            return MiddlewareResult(allow=False, reason=violation)
        acquisition_violation = _acquisition_guard_violation(call, work_item)
        if acquisition_violation is not None:
            return MiddlewareResult(allow=False, reason=acquisition_violation)
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        return result
