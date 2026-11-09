# 功能：enter_plan_mode — util 门控进入 plan 模式（镜像 exit_plan_mode 的 extra.mode_manager 协议）
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


class EnterPlanModeTool(BaseTool):
    """功能：通过 context.extra.mode_manager.switch("plan") 进入规划模式。"""

    name = "enter_plan_mode"
    description = "进入规划模式：调用 mode_manager.switch('plan')（无 mode_manager 时安全返回 switched=False）"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "进入规划模式原因"},
        },
        "required": [],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        if context is None:
            return {"switched": False, "reason": params.get("reason")}
        extra = getattr(context, "extra", None) or {}
        mode_manager = extra.get("mode_manager")
        if mode_manager is None:
            return {"switched": False, "reason": params.get("reason")}
        previous = mode_manager.switch("plan")
        return {
            "switched": True,
            "mode": "plan",
            "previous_mode": previous,
            "reason": params.get("reason"),
        }
