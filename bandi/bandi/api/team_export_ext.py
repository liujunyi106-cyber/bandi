# 功能：组织团队 → 团队模板快照（导出用）——把运行时组织还原成可再导入/再实例化的 TeamTemplate
"""端点只做取数与响应组装，口径集中在本模块，便于契约用真实组织验证。

快照只含「组织结构 + 角色配置」：角色 id / 名称 / 职责 / 汇报链 / can_spawn /
工具面 / 落盘白名单 / 每角色模型配置 / 技能绑定。不含工作项进度、产物、记忆与会话。
"""

from __future__ import annotations

from typing import Any

from bandi.company.team_template import TeamRoleSpec, TeamTemplate


class TeamExportError(Exception):
    """组织团队无法导出（团队不存在 / 没有角色 / 快照校验失败）。"""


def _strip_prefix(role_id: str, prefix: str) -> str:
    return role_id[len(prefix) :] if role_id.startswith(prefix) else role_id


def _collect_skill_refs(profile: Any) -> list[str]:
    """功能：档案里绑定的技能引用（SOP 卡 + 技能包），缺失或结构异常返回空。"""
    if profile is None:
        return []
    refs: list[str] = []
    for item in getattr(profile, "resources", []) or []:
        resource_type = str(getattr(item, "resource_type", ""))
        resource_id = str(getattr(item, "resource_id", "") or "").strip()
        if resource_type in {"skill", "general_skill"} and resource_id and resource_id not in refs:
            refs.append(resource_id)
    return refs


def build_team_template(
    *,
    team_id: str,
    team_name: str,
    roles: list[Any],
    employees: list[Any],
    profiles: dict[str, Any] | None = None,
) -> TeamTemplate:
    """功能：把某团队下的角色/员工还原成团队模板（角色 id 去 team 前缀，汇报链同步还原）。

    只收 `role.team_id == team_id` 的角色；`reports_to` / `can_spawn` 指向团队外一律丢弃
    （模板要求引用完整）。剥离前缀后若撞名，保留原 id 以免丢失角色。
    """
    normalized_team = str(team_id or "").strip()
    if not normalized_team:
        raise TeamExportError("team_id required")
    prefix = f"{normalized_team}_"
    team_roles = [
        role for role in roles if str(getattr(role, "team_id", "") or "") == normalized_team
    ]
    if not team_roles:
        raise TeamExportError(f"团队 {normalized_team!r} 没有可导出的角色")

    raw_ids = [str(getattr(role, "id", "") or "") for role in team_roles]
    stripped: dict[str, str] = {}
    used: set[str] = set()
    for raw in raw_ids:
        candidate = _strip_prefix(raw, prefix)
        if not candidate or candidate in used:
            candidate = raw
        used.add(candidate)
        stripped[raw] = candidate

    employee_by_role = {str(getattr(item, "role_id", "") or ""): item for item in employees or []}
    profiles = profiles or {}

    specs: list[TeamRoleSpec] = []
    for role in team_roles:
        raw_id = str(getattr(role, "id", "") or "")
        template_id = stripped[raw_id]
        parent = str(getattr(role, "reports_to", "") or "")
        reports_to = stripped.get(parent) if parent in stripped else None
        can_spawn = [
            stripped[spawn]
            for spawn in getattr(role, "can_spawn", []) or []
            if str(spawn) in stripped
        ]
        employee = employee_by_role.get(raw_id)
        llm_config = dict(getattr(employee, "llm_config", None) or {})
        specs.append(
            TeamRoleSpec(
                id=template_id,
                name=str(getattr(role, "name", "") or template_id),
                responsibility=str(getattr(role, "responsibility", "") or ""),
                reports_to=reports_to,
                can_spawn=can_spawn,
                tool_tags=list(getattr(role, "tool_tags", []) or []),
                is_final_decider=bool(getattr(role, "is_final_decider", False)),
                fs_scopes=list(getattr(role, "fs_scopes", []) or []),
                skill_refs=_collect_skill_refs(profiles.get(raw_id)),
                llm_config=llm_config,
            )
        )

    try:
        return TeamTemplate(
            template_id=normalized_team,
            name=str(team_name or normalized_team),
            description=f"由团队「{team_name or normalized_team}」导出的组织快照",
            roles=specs,
        )
    except ValueError as exc:  # 拓扑/引用校验失败 → 明确报错，不导出坏模板
        raise TeamExportError(f"团队快照校验失败: {exc}") from exc
