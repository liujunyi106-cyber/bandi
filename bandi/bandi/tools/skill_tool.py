from __future__ import annotations

import threading
from typing import Any

from bandi.skills.manager import create_skill_registry
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_MAX_INSTRUCTIONS_CHARS = 32 * 1024
_INSTRUCTION_TRUNCATION_NOTE = (
    f"\n\n[truncated: instructions exceeded {_MAX_INSTRUCTIONS_CHARS} chars]"
)

_REGISTRY: SkillRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def _load_registry() -> SkillRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = create_skill_registry()
    return _REGISTRY


def reset_skill_registry() -> None:
    """功能：重置技能注册表单例缓存（加锁 double-check 写口）——技能库变更
    （如蒸馏落卡）后调用，下次技能工具调用重新装载；不重置则单例缓存对新增卡片不可见。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def _truncate_instructions(instructions: str) -> tuple[str, bool]:
    """功能：技能指令截断——超 32K 字符截近 + 截断注记（防超长指令直灌模型上下文）。"""
    if len(instructions) <= _MAX_INSTRUCTIONS_CHARS:
        return instructions, False
    return instructions[:_MAX_INSTRUCTIONS_CHARS] + _INSTRUCTION_TRUNCATION_NOTE, True


def _visible_skills(
    registry: SkillRegistry, context: ToolPermissionContext | None
) -> list[SkillDef]:
    """功能：按 executor 透传的 ``extra["skill_scope"]`` 收窄可见技能。

    与 ``SkillInjector`` 同一口径（全局层 + 本团队）。作用域缺失时按**最严**
    （秘书 / global_only）处理：显式 invoke 是模型的直接调用路径，静默全放行
    等于团队边界对模型不存在。
    """
    scope: dict[str, Any] = {}
    if context is not None and isinstance(context.extra, dict):
        raw = context.extra.get("skill_scope")
        if isinstance(raw, dict):
            scope = raw
    team_id = str(scope.get("team_id") or "").strip()
    global_only = bool(scope.get("global_only"))
    if not scope:
        global_only = True
    visible = registry.collect_for_team(team_id)
    if global_only and not team_id:
        visible = [s for s in visible if not s.team_id]
    return visible


class SkillTool(BaseTool):
    name: str = "skill_invoke"
    description: str = (
        "Load a skill by name at runtime. Returns the skill's instructions, "
        "constraints, and allowed tools so the agent can follow them. "
        "Call this when a skill listed under '可用技能' in your system prompt "
        "matches the current task."
    )
    category: ToolCategory = ToolCategory.SKILLS
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill name to load, e.g. commit, review-pr",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments passed to the skill",
            },
        },
        "required": ["skill"],
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"
    is_read_only: bool = True

    def __init__(self, provider: Any | None = None) -> None:
        super().__init__()
        self._provider = provider
        self._tool_registry: Any | None = None

    def bind_registry(self, registry: Any) -> None:
        """功能：后绑定工具注册表（fork 技能的子代理工具面）"""
        self._tool_registry = registry

    async def _execute_fork(
        self,
        skill: SkillDef,
        instructions: str,
        params: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> dict[str, Any]:
        """功能：fork 技能——在独立子代理中执行，返回子代理结果"""
        if self._provider is None:
            return {
                "success": False,
                "name": skill.name,
                "forked": False,
                "error": "fork skill requires a provider (not wired)",
                "instructions": instructions,
            }
        from types import SimpleNamespace

        from bandi.agent.subagent import SubagentConfig, SubagentManager

        extra = getattr(context, "extra", None) or {}
        parent = SimpleNamespace(
            session_id=str(extra.get("session_id") or ""),
            bus=getattr(context, "event_bus", None),
            meta={},
        )
        manager = SubagentManager(
            parent_context=parent,
            provider=self._provider,
            permissions_pipeline=getattr(context, "permissions_pipeline", None),
            project_root=extra.get("project_root"),
            event_bus=getattr(context, "event_bus", None),
            role_tool_tags=extra.get("role_tool_tags"),
            role_fs_scopes=extra.get("role_fs_scopes"),
            hook_runner=extra.get("hook_runner"),
            user_id=getattr(context, "user_id", "") or "",
            policy_engine=extra.get("policy_engine"),
            parent_agent_id=str(extra.get("agent_id") or ""),
        )
        task = (
            f"按技能「{skill.name}」执行任务。\n\n{instructions}\n\n"
            f"用户参数：{params.get('args') or '(无)'}"
        )
        result = await manager.spawn(
            task,
            SubagentConfig(
                tools_allowlist=skill.allowed_tools,
                max_turns=10,
                timeout_sec=300,
                model=skill.model,
                effort=skill.effort,
            ),
            tool_registry=self._tool_registry,
        )
        return {
            "success": result.success,
            "name": skill.name,
            "forked": True,
            "summary": result.summary,
            "message": result.message,
            "session_id": result.session_id,
            "model": skill.model,
            "effort": skill.effort,
        }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        skill_name = (params.get("skill") or "").strip().lstrip("/")
        if not skill_name:
            return {"success": False, "error": "skill name is required"}
        registry = _load_registry()
        visible = _visible_skills(registry, context)
        allowed = {s.name for s in visible}
        if skill_name not in allowed:
            return {
                "success": False,
                "error": f"unknown skill: {skill_name}",
                # 功能：available 只列作用域内技能（报错路径不得回显别团队技能名）
                "available": sorted(allowed),
            }
        skill = registry.get(skill_name)
        instructions, truncated = _truncate_instructions(skill.instructions)
        if skill.fork:
            return await self._execute_fork(skill, instructions, params, context)
        payload: dict[str, Any] = {
            "success": True,
            "name": skill.name,
            "description": skill.description,
            "instructions": instructions,
            "constraints": skill.constraints,
            "allowed_tools": skill.allowed_tools,
            "args": params.get("args"),
            "fork": skill.fork,
            "model": skill.model,
            "effort": skill.effort,
        }
        if truncated:
            payload["instructions_truncated"] = True
        return payload


class SkillDiscoverTool(BaseTool):
    name: str = "skill_discover"
    description: str = (
        "List all available skills with their names, descriptions, and triggers. "
        "Optionally filter by a keyword. Use before skill_invoke to find which "
        "skill matches the current task."
    )
    category: ToolCategory = ToolCategory.SKILLS
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "Optional keyword to filter skills by name or description",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags to filter skills by (any tag match)",
            },
        },
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"
    is_read_only: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        keyword = (params.get("keyword") or "").strip().lower()
        tags = params.get("tags") or []
        registry = _load_registry()
        skills = [
            {"name": s.name, "description": s.description, "triggers": s.triggers}
            for s in _visible_skills(registry, context)
        ]
        if tags:
            tag_hits = {s.name for s in registry.list_by_tags([str(t) for t in tags])}
            skills = [s for s in skills if s["name"] in tag_hits]
        if keyword:
            skills = [
                s
                for s in skills
                if keyword in s["name"].lower() or keyword in s["description"].lower()
            ]
        return {"success": True, "count": len(skills), "skills": skills}
