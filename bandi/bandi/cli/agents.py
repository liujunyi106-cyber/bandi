# 功能：数字员工能力档案 CLI——AgentProfileManager 生产入口（建档/绑定/
# 绑定校验按资源类型惰性装配依赖：tool → factory.build_tool_registry；
# knowledge_base → knowledge.store.get_shared_store（其余命令零重装配开销）。
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from bandi.company.agent_profile import AgentProfile

agents_app = typer.Typer(help="数字员工能力档案")

_DEFAULT_PROFILES_DIR = "~/.bandi/agents/profiles"
_DEFAULT_SKILLS_DIR = "~/.bandi/skills"
_TOOL_RESOURCE_TYPE = "tool"
_KNOWLEDGE_RESOURCE_TYPE = "knowledge_base"


def _manager(
    profiles_dir: str | None,
    skills_dir: str | None,
    *,
    resource_type: str = "",
):
    from bandi.company.agent_profile import AgentProfileManager

    tool_registry = None
    knowledge_store = None
    if resource_type == _TOOL_RESOURCE_TYPE:
        from bandi.agent.factory import build_tool_registry

        tool_registry = build_tool_registry()
    elif resource_type == _KNOWLEDGE_RESOURCE_TYPE:
        from bandi.knowledge.store import get_shared_store

        knowledge_store = get_shared_store()
    return AgentProfileManager(
        profiles_dir=profiles_dir or _DEFAULT_PROFILES_DIR,
        skills_dir=skills_dir or _DEFAULT_SKILLS_DIR,
        tool_registry=tool_registry,
        knowledge_store=knowledge_store,
    )


def _echo(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False))


def _profile_brief(profile: AgentProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "employee_id": profile.employee_id,
        "name": profile.name,
        "status": profile.status,
        "persona_prompt": profile.persona_prompt,
        "resources": [
            {
                "resource_type": item.resource_type.value,
                "resource_id": item.resource_id,
                "status": item.status,
            }
            for item in profile.resources
        ],
    }


@agents_app.command("create")
def create(
    employee_id: str = typer.Argument(..., help="员工标识（= profile_id，1:1）"),
    name: str = typer.Option("", "--name", help="档案显示名"),
    persona: str = typer.Option("", "--persona", help="人设提示词"),
    description: str = typer.Option("", "--description", help="档案描述"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：建数字员工能力档案（重复建档失败）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        profile = _manager(profiles_dir, skills_dir).create_profile(
            employee_id, name=name, persona_prompt=persona, description=description
        )
    except AgentProfileError as exc:
        _echo({"employee_id": employee_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(_profile_brief(profile))


@agents_app.command("show")
def show(
    profile_id: str = typer.Argument(..., help="档案标识"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：查看档案（不存在时 exit 1）。"""
    from bandi.company.agent_profile import AgentProfileNotFoundError

    try:
        profile = _manager(profiles_dir, skills_dir).require_profile(profile_id)
    except AgentProfileNotFoundError as exc:
        _echo({"profile_id": profile_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(_profile_brief(profile))


@agents_app.command("list")
def list_cmd(
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：列出全部档案。"""
    manager = _manager(profiles_dir, skills_dir)
    _echo({"profiles": [_profile_brief(item) for item in manager.list_profiles()]})


@agents_app.command("update")
def update(
    profile_id: str = typer.Argument(..., help="档案标识"),
    name: str | None = typer.Option(None, "--name", help="新显示名"),
    persona: str | None = typer.Option(None, "--persona", help="新人设提示词"),
    status: str | None = typer.Option(None, "--status", help="active | archived"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：更新档案字段（至少一个）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        profile = _manager(profiles_dir, skills_dir).update_profile(
            profile_id, name=name, persona_prompt=persona, status=status
        )
    except AgentProfileError as exc:
        _echo({"profile_id": profile_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(_profile_brief(profile))


@agents_app.command("bind")
def bind(
    profile_id: str = typer.Argument(..., help="档案标识"),
    resource_type: str = typer.Option(
        ..., "--type", help="skill | general_skill | knowledge_base | tool"
    ),
    resource_id: str = typer.Option(..., "--resource", help="资源标识"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：绑定能力资源（绑定时即校验存在性；knowledge_base 校验知识库存在）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        profile = _manager(profiles_dir, skills_dir, resource_type=resource_type).bind_resource(
            profile_id, resource_type, resource_id
        )
    except AgentProfileError as exc:
        _echo({"profile_id": profile_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(_profile_brief(profile))


@agents_app.command("unbind")
def unbind(
    profile_id: str = typer.Argument(..., help="档案标识"),
    resource_type: str = typer.Option(..., "--type", help="资源类型"),
    resource_id: str = typer.Option(..., "--resource", help="资源标识"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：解绑能力资源（未绑定失败）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        profile = _manager(profiles_dir, skills_dir).unbind_resource(
            profile_id, resource_type, resource_id
        )
    except AgentProfileError as exc:
        _echo({"profile_id": profile_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(_profile_brief(profile))


@agents_app.command("context")
def context_cmd(
    profile_id: str = typer.Argument(..., help="档案标识"),
    session_id: str = typer.Option(..., "--session", help="会话标识"),
    request_id: str = typer.Option(..., "--request", help="请求标识"),
    trace_id: str = typer.Option("", "--trace", help="追踪标识（可选）"),
    attempt: int = typer.Option(1, "--attempt", help="尝试轮次（正整数）"),
    profiles_dir: str | None = typer.Option(None, "--profiles-dir", help="档案目录覆盖"),
    skills_dir: str | None = typer.Option(None, "--skills-dir", help="技能库目录覆盖"),
) -> None:
    """功能：为员工档案构建能力调用上下文（CapabilityContext 生产入口）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        context = _manager(profiles_dir, skills_dir).build_context(
            profile_id,
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id or None,
            attempt=attempt,
        )
    except (AgentProfileError, ValueError) as exc:
        _echo({"profile_id": profile_id, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    _echo(
        {
            "agent_id": context.agent_id,
            "session_id": context.session_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "attempt": context.attempt,
        }
    )
