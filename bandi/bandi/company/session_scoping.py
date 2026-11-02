# 功能：会话作用域——scope key 生成与解析

from __future__ import annotations

from dataclasses import dataclass

# 成员执行会话 id 前缀（生成/解析单一约定，防散落字面量）
MEMBER_SESSION_PREFIX = "role-session::"


@dataclass(frozen=True)
class MemberSessionParts:
    """功能：成员会话 id 的解析结果——`project` 保留原值（含 "default"），
    `role`/`employee` 为倒数两段；`middle` 为 project 与 role 之间的段
    （scope/team 形态不区分——两者解析需求相同）。"""

    project: str
    role: str
    employee: str
    middle: tuple[str, ...] = ()


def parse_member_session_id(session_id: str | None) -> MemberSessionParts | None:
    """功能：成员执行会话 id 的统一解析入口（生成侧同文件成对）。

    形态：`role-session::<project>[::scope][::team]::<role>::<employee>`。
    兼容旧实现的畸形边界：单段取 project、两段取倒数第二段为 role；
    非该前缀返回 None。解析结果逐例与收敛前的两份散落实现一致。
    """
    text = str(session_id or "").strip()
    if not text.startswith(MEMBER_SESSION_PREFIX):
        return None
    tail = text[len(MEMBER_SESSION_PREFIX) :]
    parts = [part.strip() for part in tail.split("::")]
    if not parts or not parts[0]:
        return None
    project = parts[0]
    if len(parts) == 1:
        return MemberSessionParts(project=project, role="", employee="")
    role = parts[-2]
    employee = parts[-1]
    middle = tuple(parts[1:-2])
    return MemberSessionParts(project=project, role=role, employee=employee, middle=middle)


def is_member_session_id(session_id: str | None) -> bool:
    """功能：是否成员执行会话 id（前缀判定统一入口）。"""
    return str(session_id or "").strip().startswith(MEMBER_SESSION_PREFIX)


def member_session_prefix(project_id: str) -> str:
    """功能：某项目的成员会话 id 前缀（产物/用量前缀扫描用）。"""
    return f"{MEMBER_SESSION_PREFIX}{str(project_id or '').strip()}::"


def role_from_session_id(session_id: str) -> str:
    """功能：从成员会话 id 反解角色（倒数第二段）；非该形态返回空串。"""
    parts = parse_member_session_id(session_id)
    return parts.role if parts is not None else ""


def scoped_member_session_id(
    project_id: str,
    session_scope_id: str,
    role_id: str,
    employee_id: str,
    team_instance_id: str = "",
    explicit_id: str = "",
    history_scope_id: str = "",
) -> str:
    explicit = explicit_id.strip()
    if explicit:
        return explicit
    project = project_id.strip() or "default"
    # history_scope_id 非空 = 员工持久历史空间（跨任务共享会话）；
    # 缺省回退 session_scope_id（任务级旧行为）。本参数**只影响会话拼接**——
    # 队列 frontier tag 走 `scoped_queue_key`（不接收本参数）。
    scope = history_scope_id.strip() or session_scope_id.strip()
    role = role_id.strip() or "unknown"
    employee = employee_id.strip() or "default"
    team_instance = team_instance_id.strip()
    scoped_prefix = f"{project}::{scope}" if scope else project
    parts = [scoped_prefix]
    if team_instance:
        parts.append(team_instance)
    parts.append(role)
    parts.append(employee)
    return MEMBER_SESSION_PREFIX + "::".join(parts)


def project_from_session_id(session_id: str) -> str:
    """功能：从成员会话 id 反解项目归属（`role-session::<项目>::…`）；非该形态返回空串。

    与 `scoped_member_session_id` 单一约定成对（生成/解析同文件，统一经
    `parse_member_session_id`）。缺省项目（生成侧 `project_id` 空时写 "default"）
    视为**无归属**，返回空串而非 "default" 避免把「company 空间」误当成一个叫
    default 的项目。
    """
    parts = parse_member_session_id(session_id)
    if parts is None or parts.project == "default":
        return ""
    return parts.project


def scoped_queue_key(
    session_scope_id: str,
    role_id: str = "",
    team_instance_id: str = "",
) -> str:
    role = role_id.strip()
    scope = session_scope_id.strip()
    team_instance = team_instance_id.strip()
    if not role:
        return ""
    parts: list[str] = []
    if scope:
        parts.append(scope)
    if team_instance:
        parts.append(team_instance)
    parts.append(role)
    return "::".join(parts)


def task_session_scope_id(metadata: dict | None) -> str:
    if not metadata:
        return ""
    return str(
        metadata.get("parent_session_id", "") or metadata.get("session_id", "") or ""
    ).strip()
