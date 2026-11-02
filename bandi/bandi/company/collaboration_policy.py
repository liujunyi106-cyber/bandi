from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bandi.company._utils import path_within
from bandi.company.org_engine import OrgEngine

_COLLABORATION_TOOLS = {
    "send_dm",
    "broadcast_issue",
    "start_meeting",
    "ask_peer_and_wait",
    "inbox",
    "reply_message",
    "respond_meeting",
    "route_work",
    "close_human_review",
}

_WRITE_TOOLS = {
    "file_write",
    "file_edit",
    "apply_patch",
    "shell_exec",
    "write",
    "edit",
    "bash",
    "python_exec",
    "todo_write",
}

_SHELL_REDIRECT_RE = re.compile(r"(?:>>?|\d*&?>|>\s*&)\s*([^\s|&;\"']+)")

SCOPE_ASSIGNED_WORKSPACE = "assigned_workspace"
SCOPE_READ_ONLY = "read_only"
EXECUTION_MODE_COMPANY = "company_mode"
MAX_ALLOWED_TARGETS_DISPLAY = 12
MAX_DOWNSTREAM_DISPLAY = 8


class CollaborationAction(StrEnum):
    SEND_DM = "send_dm"
    BROADCAST = "broadcast"
    START_MEETING = "start_meeting"
    ASSIGN_TASK = "assign_task"
    REQUEST_REVIEW = "request_review"


@dataclass(frozen=True)
class CollaborationRule:
    from_role: str
    to_role: str
    allowed_actions: frozenset[CollaborationAction] = field(
        default_factory=lambda: frozenset({CollaborationAction.SEND_DM})
    )
    auto_approve: bool = False

    def allows(self, action: CollaborationAction) -> bool:
        return action in self.allowed_actions


def build_contact_policy(org: OrgEngine) -> list[CollaborationRule]:
    rules: list[CollaborationRule] = []
    agents = org.list_agents()
    agent_map = {a.role_id: a for a in agents}
    by_reports_to: dict[str, list[str]] = {}
    for agent in agents:
        by_reports_to.setdefault(agent.reports_to or "", []).append(agent.role_id)
    for agent in agents:
        reports_to = agent.reports_to or ""
        if reports_to and reports_to in agent_map:
            rules.append(
                CollaborationRule(
                    from_role=agent.role_id,
                    to_role=reports_to,
                    allowed_actions=frozenset(CollaborationAction),
                    auto_approve=True,
                )
            )
        for sub in by_reports_to.get(agent.role_id, ()):
            rules.append(
                CollaborationRule(
                    from_role=agent.role_id,
                    to_role=sub,
                    allowed_actions=frozenset(CollaborationAction),
                    auto_approve=True,
                )
            )
        for peer in by_reports_to.get(reports_to, ()):
            if peer == agent.role_id:
                continue
            rules.append(
                CollaborationRule(
                    from_role=agent.role_id,
                    to_role=peer,
                    allowed_actions=frozenset(
                        {CollaborationAction.SEND_DM, CollaborationAction.START_MEETING}
                    ),
                    auto_approve=False,
                )
            )
    return rules


def _normalized_role_list(values: list[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values or []:
        role_id = str(item or "").strip()
        if not role_id or role_id in seen:
            continue
        seen.add(role_id)
        result.append(role_id)
    return result


def _work_item_metadata(work_item: Any | None) -> dict[str, Any]:
    if work_item is None:
        return {}
    return dict(getattr(work_item, "metadata", {}) or {})


def collect_dynamic_contact_roles(work_item: Any | None) -> list[str]:
    """Collect collaboration targets from the work item ownership contract.

    work_item_gate / dynamic_allowed_contact_roles / handoff_log 收集动态角色。
    """
    if work_item is None:
        return []
    metadata = _work_item_metadata(work_item)
    ownership_contract = dict(metadata.get("ownership_contract", {}) or {})
    work_item_gate = dict(metadata.get("work_item_gate", {}) or {})
    dynamic: list[str] = []
    dynamic.extend(_normalized_role_list(ownership_contract.get("allowed_collaboration_targets")))
    dynamic.extend(_normalized_role_list(ownership_contract.get("downstream_consumer")))
    dynamic.extend(_normalized_role_list(metadata.get("dynamic_allowed_contact_roles")))
    reviewer_role = str(
        work_item_gate.get("reviewer_role", "") or metadata.get("manager_role_id", "") or ""
    ).strip()
    if reviewer_role:
        dynamic.append(reviewer_role)
    for handoff in list(metadata.get("handoff_log", []) or []):
        if isinstance(handoff, dict):
            dynamic.append(str(handoff.get("to", "")).strip())
            dynamic.append(str(handoff.get("from", "")).strip())
    return _normalized_role_list(dynamic)


def effective_contact_roles(
    role_id: str,
    *,
    org: OrgEngine | None = None,
    rules: list[CollaborationRule] | None = None,
    work_item: Any | None = None,
    org_engine: Any | None = None,
) -> list[str]:
    """Static topology (rules or org engine) merged with dynamic contract roles.

    回退无参调用）；org/rules 为 Bandi 旧 API 兼容参数。
    """
    static: list[str] = []
    if rules is not None:
        for rule in rules:
            if rule.from_role == role_id:
                static.append(rule.to_role)
    elif org is not None:
        static = org.get_allowed_contact_roles(role_id)
    elif org_engine is not None:
        getter = getattr(org_engine, "get_allowed_contact_roles", None)
        if getter is not None:
            try:
                static = list(getter(role_id, task=work_item) or [])
            except TypeError:
                static = list(getter(role_id) or [])
    return _normalized_role_list([*static, *collect_dynamic_contact_roles(work_item)])


def collaboration_allowed(
    from_role: str,
    to_role: str,
    action: CollaborationAction = CollaborationAction.SEND_DM,
    *,
    org: OrgEngine | None = None,
    rules: list[CollaborationRule] | None = None,
) -> bool:
    if rules is None and org is not None:
        rules = build_contact_policy(org)
    if rules is not None:
        for rule in rules:
            if rule.from_role == from_role and rule.to_role == to_role:
                if rule.allows(action):
                    return True
        return False
    if org is not None:
        return to_role in org.get_allowed_contact_roles(from_role)
    return False


def render_collaboration_context(role_id: str, *, org: OrgEngine | None = None) -> str:
    contacts = effective_contact_roles(role_id, org=org)
    if not contacts:
        return ""
    parts = ["## Collaboration Context", f"Your role: {role_id}", "Contactable roles:"]
    parts.extend(f"- {r}" for r in contacts)
    return "\n".join(parts)


def _command_head_is(command: str, names: tuple[str, ...]) -> bool:
    """判断命令首词是否匹配给定命令名（管道/分号开头命令仍算，头部即写命令）。"""
    stripped = command.strip()
    if not stripped:
        return False
    try:
        head = shlex.split(stripped)[0]
    except ValueError:
        return False
    return head.lower() in names


def _candidate_paths(tool_name: str, arguments: dict[str, Any]) -> list[Path]:
    if tool_name in {"file_write", "file_edit"}:
        path = str(arguments.get("path", "") or arguments.get("directory", "") or "").strip()
        return [Path(path)] if path else []
    if tool_name == "apply_patch":
        patch = str(arguments.get("patch", "") or "").splitlines()
        paths: list[Path] = []
        prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")
        for line in patch:
            for prefix in prefixes:
                if line.startswith(prefix):
                    raw = line[len(prefix) :].strip()
                    if raw:
                        paths.append(Path(raw))
        return paths
    if tool_name in {"shell_exec", "bash", "python_exec"}:
        paths: list[Path] = []
        # _resolve_working_directory 推断链）
        cwd = str(
            arguments.get("cwd")
            or arguments.get("working_directory")
            or arguments.get("workdir")
            or ""
        ).strip()
        if not cwd:
            from bandi.tools.fs_tool import workspace_root

            try:
                cwd = str(workspace_root())
            except Exception:  # noqa: BLE001 — 无法推断时保持原行为（不阻断调用）
                cwd = ""
        if cwd:
            paths.append(Path(cwd))
        command = str(arguments.get("command", "") or "")
        for match in _SHELL_REDIRECT_RE.finditer(command):
            target = str(match.group(1) or "").strip()
            if not target or target.isdigit():
                continue
            # 误判会拒绝合法命令（假阳性）；仅真实文件重定向才检查越界
            if target == "/dev/null":
                continue
            candidate = Path(target)
            if not candidate.is_absolute() and cwd:
                candidate = Path(cwd) / candidate
            paths.append(candidate)
        # 漏检。用 shlex 分词（引号内目标不拆碎）；分词失败则跳过（保持原行为）
        if _command_head_is(command, ("mv", "cp", "rm")):
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = []
            if tokens:
                cmd = tokens[0].lower()
                positional = [t for t in tokens[1:] if not t.startswith("-") and t != "--"]
                if cmd == "rm":
                    candidates = positional
                elif positional:
                    candidates = [positional[-1]]
                else:
                    candidates = []
                for raw in candidates:
                    candidate = Path(raw)
                    if not candidate.is_absolute() and cwd:
                        candidate = Path(cwd) / candidate
                    paths.append(candidate)
        return paths
    if tool_name in {"write", "edit"}:
        path = str(arguments.get("path", "") or "").strip()
        return [Path(path)] if path else []
    return []


def _resolved_write_roots(work_item: Any | None) -> list[Path]:
    if work_item is None:
        return []
    metadata = _work_item_metadata(work_item)
    ownership_contract = dict(metadata.get("ownership_contract", {}) or {})
    scope = str(ownership_contract.get("write_scope", "") or "").strip()
    roots: list[Path] = []
    if scope and scope not in {SCOPE_ASSIGNED_WORKSPACE, SCOPE_READ_ONLY}:
        roots.append(Path(scope))
    if scope == SCOPE_ASSIGNED_WORKSPACE:
        from bandi.company.data_acquisition_policy import workspace_root_for_task

        workspace_root = workspace_root_for_task(work_item)
        if workspace_root is not None:
            roots.append(workspace_root)
    output_dir = str(metadata.get("target_output_dir", "") or "").strip()
    if output_dir:
        roots.append(Path(output_dir))
    return [path.resolve() for path in roots if str(path).strip()]


def _work_item_role_id(work_item: Any | None) -> str:
    if work_item is None:
        return ""
    metadata = _work_item_metadata(work_item)
    return str(
        getattr(work_item, "assignee_role", "") or metadata.get("work_item_role_id", "") or ""
    ).strip()


def ownership_guard_violation(
    *,
    work_item: Any | None,
    tool_name: str,
    arguments: dict[str, Any],
    org_engine: Any | None = None,
) -> str | None:
    """Enforce the ownership contract against a tool invocation.

    write_scope 越界 + 协作工具收件人白名单。
    """
    if work_item is None:
        return None
    metadata = _work_item_metadata(work_item)
    if str(metadata.get("execution_mode", "") or "").strip() != EXECUTION_MODE_COMPANY:
        return None
    ownership_contract = dict(metadata.get("ownership_contract", {}) or {})
    if not ownership_contract:
        return None
    if tool_name in _WRITE_TOOLS:
        write_scope = str(ownership_contract.get("write_scope", "") or "").strip()
        if write_scope == "read_only":
            return (
                "Ownership contract blocks write-side effects for this work item. "
                "This work item is read_only and must not modify files or run mutating shell commands."
            )
        roots = _resolved_write_roots(work_item)
        candidate_paths = _candidate_paths(tool_name, arguments)
        if candidate_paths and not roots:
            return (
                "Ownership contract write_scope cannot be resolved to a workspace "
                "root for this work item. Write-side effects are blocked (fail-closed)."
            )
        if roots and candidate_paths:
            out_of_scope = [
                str(path)
                for path in candidate_paths
                if not any(path_within(path, root) for root in roots)
            ]
            if out_of_scope:
                return (
                    "Ownership contract blocks writes outside the assigned workspace. "
                    f"Out-of-scope target(s): {', '.join(out_of_scope[:4])}."
                )
    if tool_name in _COLLABORATION_TOOLS:
        from_role = _work_item_role_id(work_item)
        allowed = set(
            effective_contact_roles(from_role, work_item=work_item, org_engine=org_engine)
        )
        recipients: list[str] = []
        if tool_name == "broadcast_issue":
            recipients = _normalized_role_list(arguments.get("to_agents"))
        elif tool_name == "start_meeting":
            recipients = _normalized_role_list(arguments.get("participants"))
        elif tool_name == "route_work":
            recipients = _normalized_role_list([arguments.get("target_role")])
        elif tool_name == "close_human_review":
            recipients = _normalized_role_list([arguments.get("target_role") or "owner"])
        else:
            recipients = _normalized_role_list([arguments.get("to_agent")])
        invalid = [recipient for recipient in recipients if recipient not in allowed]
        if invalid:
            return (
                "Ownership contract / collaboration topology blocks this contact target. "
                f"Recipient(s) not allowed for the current work item: {', '.join(invalid)}."
            )
    return None


def render_ownership_contract(work_item: Any | None) -> str:
    """Render the boundary-enforcing part of the ownership contract.

    collaboration targets / downstream consumers；summary/expected_artifacts
    不渲染（与 work item identity 重复）。
    """
    if work_item is None:
        return ""
    metadata = _work_item_metadata(work_item)
    ownership_contract = dict(metadata.get("ownership_contract", {}) or {})
    if not ownership_contract:
        return ""
    lines: list[str] = []
    write_scope = str(ownership_contract.get("write_scope", "") or "").strip()
    if write_scope:
        lines.append(f"Write scope: {write_scope}")
    allowed_collaboration_targets = _normalized_role_list(
        ownership_contract.get("allowed_collaboration_targets")
    )
    if allowed_collaboration_targets:
        lines.append("Allowed collaboration targets:")
        lines.extend(
            f"- {item}" for item in allowed_collaboration_targets[:MAX_ALLOWED_TARGETS_DISPLAY]
        )
    downstream = _normalized_role_list(ownership_contract.get("downstream_consumer"))
    if downstream:
        lines.append("Downstream consumers:")
        lines.extend(f"- {item}" for item in downstream[:MAX_DOWNSTREAM_DISPLAY])
    if not lines:
        return ""
    return "## Ownership Contract\n" + "\n".join(lines)
