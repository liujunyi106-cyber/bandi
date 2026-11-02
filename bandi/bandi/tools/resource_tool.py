# 功能：资源管理工具（会话内创建技能 / SOP 卡片，并按需绑定员工）。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.resources import catalog
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


class ResourceConfigureTool(BaseTool):
    name: str = "resource_configure"
    description: str = (
        "Create or update a skill or SOP card, and optionally bind it to an employee. "
        "Use kind=skill with Markdown content, or kind=sop with a SkillCard JSON document. "
        "When the user says '给我用' or '请你给自己配置', pass employee or rely on the "
        "current employee context."
    )
    category: ToolCategory = ToolCategory.SKILLS
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["skill", "sop"]},
            "name": {
                "type": "string",
                "description": "Resource name",
                "aliases": ["title", "file", "filename"],
            },
            "content": {
                "type": "string",
                "description": "Skill Markdown or SOP card JSON",
                "aliases": ["text", "body", "markdown"],
            },
            "employee": {
                "type": "string",
                "description": "Optional employee name or id to bind the resource to",
                "aliases": ["employee_id", "assignee"],
            },
        },
        "required": ["kind", "name"],
    }
    param_aliases: dict[str, str] = {
        "title": "name",
        "file": "name",
        "filename": "name",
        "text": "content",
        "body": "content",
        "markdown": "content",
        "employee_id": "employee",
        "assignee": "employee",
    }
    tags: list[str] = ["skills", "code", "util"]
    risk_level: str = "medium"
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        kind = str(params.get("kind") or "").strip().lower()
        name = str(params.get("name") or "").strip()
        content = str(params.get("content") or "")
        if kind not in {"skill", "sop"}:
            return {"success": False, "error": "kind 必须是 skill / sop"}
        if not name:
            return {"success": False, "error": "name 不能为空"}
        employee_id, employee_error = _resolve_employee(
            str(params.get("employee") or "").strip(), context
        )
        if employee_error:
            return {"success": False, "error": employee_error}
        try:
            store = _knowledge_store()
            if kind == "skill":
                item = _upsert_skill(name, content, store)
                bind_type = "general_skill"
            else:
                item = _upsert_sop(name, content, store)
                bind_type = "skill"
            bound_to = ""
            if employee_id:
                _bind(employee_id, bind_type, item["id"])
                bound_to = employee_id
            _reset_skill_cache()
            return {
                "success": True,
                "kind": kind,
                "id": item["id"],
                "title": item["title"],
                "path": item["path"],
                "bound_to": bound_to,
            }
        except catalog.ResourceError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 工具异常不阻断 Agent 循环
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _knowledge_store() -> Any:
    from bandi.knowledge.store import get_shared_store

    return get_shared_store()


def _resolve_employee(explicit: str, context: ToolPermissionContext | None) -> tuple[str, str]:
    current = ""
    if context is not None and context.extra:
        current = str(context.extra.get("employee_id") or "").strip()
    if explicit and explicit == current:
        return current, ""
    if explicit:
        found = _find_org_employee(explicit)
        if found:
            return found, ""
        return "", f"未找到员工: {explicit}"
    return current, ""


def _find_org_employee(value: str) -> str:
    try:
        from bandi.company.org_engine import OrgEngine
        from bandi.company.org_persistence import load_org
        from bandi.company.recruiter import Recruiter

        org = OrgEngine()
        recruiter = Recruiter()
        load_org(org, recruiter)
        for employee in org.list_employees(include_inactive=True):
            if value in {str(employee.employee_id), str(employee.name)}:
                return str(employee.employee_id)
    except Exception:  # noqa: BLE001 - 组织读失败时返回未找到
        return ""
    return ""


def _upsert_skill(name: str, content: str, store: Any) -> dict[str, Any]:
    try:
        existing = catalog.get_resource("skill", name, store)
    except catalog.ResourceNotFoundError:
        existing = None
    if existing is not None:
        if content.strip():
            return catalog.save_resource("skill", name, content, store)
        return existing["item"]
    if not content.strip():
        raise catalog.ResourceValidationError("创建技能需要 content")
    return catalog.import_skill(content, f"{name}.md", name=name)


def _upsert_sop(name: str, content: str, store: Any) -> dict[str, Any]:
    try:
        existing = catalog.get_resource("sop", name, store)
    except catalog.ResourceNotFoundError:
        existing = None
    if existing is not None:
        if content.strip():
            return catalog.save_resource("sop", name, content, store)
        return existing["item"]
    if not content.strip():
        raise catalog.ResourceValidationError("创建 SOP 需要 content（SkillCard JSON）")
    return catalog.import_sop(content)


def _bind(employee_id: str, resource_type: str, resource_id: str) -> None:
    from bandi.company.agent_profile import AgentProfileManager

    manager = AgentProfileManager(
        profiles_dir=str(Path.home() / ".bandi" / "agents" / "profiles"),
        skills_dir=str(catalog.skills_root()),
    )
    profile = manager.get_profile(employee_id)
    if profile is None:
        profile = manager.create_profile(employee_id, name=employee_id)
    if any(
        str(item.resource_type) == resource_type and str(item.resource_id) == resource_id
        for item in profile.resources
    ):
        return
    try:
        manager.bind_resource(employee_id, resource_type, resource_id)
    except Exception as exc:
        raise catalog.ResourceValidationError(f"绑定员工失败: {exc}") from exc


def _reset_skill_cache() -> None:
    try:
        from bandi.tools.skill_tool import reset_skill_registry

        reset_skill_registry()
    except Exception:  # noqa: BLE001 - 缓存刷新失败不影响已落盘资源
        return
