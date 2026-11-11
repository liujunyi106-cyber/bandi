# 功能：群聊目标工具——顶层角色在群会话里维护目标账本
from __future__ import annotations

from typing import Any

from bandi.company.group_goal_registry import get_group_goal_service
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_ACTIONS = [
    "show",
    "draft",
    "start",
    "revise",
    "task",
    "pass",
    "rework",
    "complete",
    "pause",
    "resume",
    "stop",
]


class GroupGoalTool(BaseTool):
    name: str = "group_goal"
    description: str = (
        "Set and run the current group chat's goal: one outcome, how to check it is done, "
        "and ordered stages whose tasks go to named members. Only the group's top-level role "
        "changes it; any member can show it. Use draft to align, start to write and begin "
        "stage 1, revise when the user corrects direction, pass or rework to check a stage, "
        "and complete when every stage passed. Evidence is required for pass and complete."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Goal operation",
            },
            "title": {"type": "string", "description": "Short goal title, at most 80 chars"},
            "brief": {
                "type": "string",
                "description": "draft: what the user wants, in their words",
            },
            "objective": {"type": "string", "description": "start/revise: one outcome"},
            "acceptance": {
                "type": "string",
                "description": "start/revise: how the whole goal is checked",
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "start/revise: rules every member keeps",
            },
            "stages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "acceptance": {"type": "string"},
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "bot": {"type": "string"},
                                    "task": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "description": "start/revise: stages in order, each with tasks for members",
            },
            "bot": {"type": "string", "description": "task/rework: member role id or name"},
            "stage": {"type": "integer", "description": "task: stage number, 1-based"},
            "status": {
                "type": "string",
                "enum": ["pending", "working", "reported", "done"],
                "description": "task: new task status",
            },
            "note": {"type": "string", "description": "task: short status note"},
            "evidence": {"type": "string", "description": "pass/complete: proof"},
            "reason": {"type": "string", "description": "revise/rework/stop: why"},
            "summary": {"type": "string", "description": "complete: what was delivered"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    needs_context: bool = True
    risk_level: str = "low"
    is_concurrency_safe: bool = False

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        service = get_group_goal_service()
        if service is None:
            return {"success": False, "error": "群聊目标服务未装配"}
        extra = getattr(context, "extra", None) or {}
        session_id = str(
            extra.get("group_conversation_id") or extra.get("session_id") or ""
        ).strip()
        role_id = str(getattr(context, "role", "") or "").strip()
        if not session_id:
            return {"success": False, "error": "当前会话没有 session_id"}
        if not role_id:
            return {"success": False, "error": "当前会话没有角色身份"}
        payload = {key: value for key, value in params.items() if key != "action"}
        payload["project_id"] = str(extra.get("project_id") or "")
        try:
            result = service.apply_action(
                session_id,
                role_id,
                str(params.get("action") or ""),
                payload,
            )
        except Exception as exc:  # noqa: BLE001 — 工具失败不阻断 Agent 循环
            return {"success": False, "error": str(exc)}
        return {"success": True, **result}
