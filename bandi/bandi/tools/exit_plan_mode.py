from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


class ExitPlanModeTool(BaseTool):
    name: str = "exit_plan_mode"
    description: str = (
        "Request to exit plan mode and start executing. Prompts the user for "
        "approval; on approval the agent switches to execute mode with full "
        "tool access. Use only after the plan is ready for review."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short summary of the plan to show the user",
            },
        },
    }
    risk_level: str = "medium"
    is_read_only: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        extra = getattr(context, "extra", None) or {}
        mode_manager = extra.get("mode_manager")
        if mode_manager is None:
            return {"error": "mode manager not bound (standalone invocation)"}
        if mode_manager.mode != "plan":
            return {
                "switched": False,
                "mode": mode_manager.mode,
                "message": "not in plan mode",
            }
        pipeline = getattr(context, "permissions_pipeline", None)
        approved = False
        if pipeline is not None and hasattr(pipeline, "confirm_tool"):
            # fail-closed：无确认通道（headless）或用户拒绝均保持 plan
            approved = await pipeline.confirm_tool(
                tool_name=self.name,
                arguments=params,
                risk=self.risk_level,
                reason="exit plan mode requires user approval",
            )
        if not approved:
            return {
                "switched": False,
                "mode": "plan",
                "message": (
                    "user declined (or no confirmation channel available); stay in plan mode"
                ),
            }
        previous = mode_manager.switch("execute")
        return {"switched": True, "previous_mode": previous, "mode": mode_manager.mode}
