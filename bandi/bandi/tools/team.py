# 功能：团队工具——team_list（列出可用团队模板）+ team_create（按模板一键实例化 AI 员工团队）。
# 装配模式对齐 spawn_thread_task：build_tool_registry 裸构造注册，公司/API 链路在
# agent_runner 处 bind_deps 后绑定 org_engine；未绑定 → fail-closed（不裸崩）。
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from bandi.tools.base import (
    BaseTool,
    RetryPolicy,
    ToolCategory,
    ToolPermissionContext,
)


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


class TeamListTool(BaseTool):
    """功能：列出可用团队模板（template_id/名称/角色数）——团队创建的前置查询。"""

    name: str = "team_list"
    description: str = (
        "List available team templates for building an AI employee team "
        "(returns template_id, name and role count). Call this before team_create."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        from bandi.company.team_template import TeamTemplateError
        from bandi.company.team_template_store import TeamTemplateStore

        try:
            templates = TeamTemplateStore().list_templates()
        except (TeamTemplateError, OSError) as exc:
            return _err(f"team_list failed: {exc}")
        items = [
            {
                "template_id": str(template.template_id),
                "name": str(template.name),
                "role_count": len(getattr(template, "roles", None) or []),
            }
            for template in templates
        ]
        return {"success": True, "templates": items, "count": len(items)}


class TeamCreateTool(BaseTool):
    """功能：按团队模板实例化（注册角色/员工 1:1/席位/profiles），返回实例化报告。"""

    name: str = "team_create"
    description: str = (
        "Create an AI employee team from a team template: registers prefixed roles, "
        "1:1 employees, seats and profiles, then returns an instantiation report "
        "(team_id, counts, warnings). Use team_list first to discover template_id."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "团队模板 id（先用 team_list 查询）"},
            "team_id": {
                "type": "string",
                "description": "可选团队 id（缺省沿用模板 id）；角色/员工将以该前缀注册",
            },
        },
        "required": ["template_id"],
    }
    # 组织写入（角色/员工/席位注册）——批次串行化，避免并发重入
    is_concurrency_safe: bool = False
    risk_level: str = "medium"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self, org_engine: Any | None = None) -> None:
        self.org_engine = org_engine

    def bind_deps(self, org_engine: Any | None = None) -> None:
        """后绑定组织环境（公司/API 链路装配点注入；None 保持 fail-closed）。"""
        if org_engine is not None:
            self.org_engine = org_engine

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        if self.org_engine is None:
            return _err(
                "team_create requires org_engine（当前会话未连接组织环境，"
                "请在已接入组织/项目的会话中使用）"
            )
        template_id = str(params.get("template_id") or "").strip()
        if not template_id:
            return _err("template_id is required")
        from bandi.company.agent_profile import AgentProfileManager
        from bandi.company.team_template import TeamTemplateError
        from bandi.company.team_template_store import TeamTemplateStore, build_org_from_template

        try:
            template = TeamTemplateStore().get_template(template_id)
        except (TeamTemplateError, OSError) as exc:
            return _err(f"team_create failed: {exc}")
        if template is None:
            return _err(f"team template not found: {template_id}")
        team_id = str(params.get("team_id") or "").strip()
        if team_id:
            template = template.model_copy(update={"team_id": team_id})
        try:
            report = build_org_from_template(
                template,
                self.org_engine,
                profile_manager=AgentProfileManager(),
            )
        except (TeamTemplateError, ValueError) as exc:
            return _err(f"team_create failed: {exc}")
        return {"success": True, "report": asdict(report)}
