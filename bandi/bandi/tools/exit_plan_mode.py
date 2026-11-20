# 功能：exit_plan_mode — 提交完整计划请求用户审批，批准后切到执行模式
from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

# 功能：计划正文必须以 markdown 一级标题开头——便于审批卡渲染标题、也强制模型
# 交出可评审的成形计划，而不是一句「我要开始执行了」。
_PLAN_HEADING_RE = re.compile(r"^#\s+\S")

# 功能：审批卡里计划正文的最大展示长度；超出由调用方自行看长文本通道
_MAX_PLAN_CHARS = 8000


def _plan_heading(plan: str) -> str:
    """功能：取计划的一级标题作为审批卡标题（无标题时回退通用文案）。"""
    for line in plan.splitlines():
        stripped = line.strip()
        if _PLAN_HEADING_RE.match(stripped):
            return stripped.lstrip("#").strip()
    return "执行计划"


class ExitPlanModeTool(BaseTool):
    name: str = "exit_plan_mode"
    description: str = (
        "Use only in plan mode. Present the completed plan for the user's review "
        "and, on approval, leave plan mode. The user may approve (carry out the plan "
        "from your next step) or keep planning — their feedback comes back in the "
        "tool result; revise and present again."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "The complete plan, as markdown, starting with a # heading that names it."
                ),
            },
        },
        "required": ["plan"],
    }
    risk_level: str = "medium"
    is_read_only: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        """功能：校验计划 → 走用户审批通道 → 批准则切 execute，否则回可操作反馈。

        反馈必须让模型能自纠：只说「用户拒绝」会让它原样重试同一份计划，
        真机表现为同一工具反复失败直到回合预算耗尽。
        """
        plan = str(params.get("plan") or "").strip()
        if not plan:
            return {
                "error": "plan is required",
                "next_step": (
                    "把完整计划作为 markdown 正文放进 plan 参数（以 # 标题开头）后再调用。"
                    "本工具不负责进入计划模式，进入请用 enter_plan_mode。"
                ),
            }
        if not _PLAN_HEADING_RE.match(plan.splitlines()[0].strip()):
            return {
                "error": "plan must start with a markdown # heading",
                "next_step": (
                    "给计划加一行一级标题（如 `# 重构检索层`），正文写在标题下面，"
                    "按步骤/风险/验证方式分节后再提交。"
                ),
            }

        extra = getattr(context, "extra", None) or {}
        mode_manager = extra.get("mode_manager")
        if mode_manager is None:
            return {
                "error": "mode manager not bound",
                "next_step": "当前会话无法切换模式，请让用户手动切换；不要重复调用本工具。",
            }
        if mode_manager.mode != "plan":
            return {
                "switched": False,
                "mode": mode_manager.mode,
                "message": "not in plan mode",
            }

        pipeline = getattr(context, "permissions_pipeline", None)
        approved = False
        if pipeline is not None and hasattr(pipeline, "confirm_tool"):
            # reason 同时充当审批卡正文：用户必须能看到计划本身才能评审
            approved = await pipeline.confirm_tool(
                tool_name=self.name,
                arguments=params,
                risk=self.risk_level,
                reason=f"批准执行此计划（{_plan_heading(plan)}）？\n\n{plan[:_MAX_PLAN_CHARS]}",
            )
        if not approved:
            return {
                "switched": False,
                "mode": "plan",
                "approved": False,
                "next_step": (
                    "用户未批准，仍在计划模式。禁止用同一份计划重复调用本工具——"
                    "按用户意见修订计划后重新提交；若用户已明确改主意，"
                    "先问明要保留哪些步骤再提交。"
                ),
            }
        previous = mode_manager.switch("execute")
        return {
            "switched": True,
            "previous_mode": previous,
            "mode": mode_manager.mode,
            "approved": True,
            "next_step": "计划已批准，从下一步开始按计划执行。",
        }
