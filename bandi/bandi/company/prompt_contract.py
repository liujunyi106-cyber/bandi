# 功能：提示词契约——v2 结构化 assignment 构建/校验/渲染

from __future__ import annotations

import copy
from typing import Any

from bandi.company._utils import as_mapping as _as_mapping

PROMPT_CONTRACT_VERSION = 2


def _work_item_kind(work_item: Any) -> str:
    kind = str(getattr(work_item, "kind", "") or "").strip()
    if not kind:
        turn = getattr(work_item, "turn_type", None)
        kind = turn.value if hasattr(turn, "value") else ""
    return str(kind or "").strip()


def build_contract(
    work_item_id: str,
    title: str,
    turn_type: str,
    phase: str,
    assignee_role: str | None = None,
    description: str = "",
    parent_context: str | None = None,
    dependencies: list[str] | None = None,
    output_format: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """[deprecated] 旧式 9 字段契约构造器。

    与 :func:`make_prompt_contract` / :func:`normalize_prompt_contract`
    （v2 主用 API）并存仅为兼容既有调用；新代码请使用 v2 API。
    """
    contract: dict[str, Any] = {
        "version": PROMPT_CONTRACT_VERSION,
        "work_item_id": work_item_id,
        "title": title,
        "turn_type": turn_type,
        "phase": phase,
        "assignee_role": assignee_role or "",
        "task_brief": description or "",
        "parent_context": parent_context or "",
        "dependencies": dependencies or [],
        "output_format": output_format or "",
    }
    if extra:
        contract["extra"] = copy.deepcopy(extra)
    return contract


def render_contract(contract: dict[str, Any]) -> str:
    """[deprecated] 旧式契约渲染（v1 9 字段格式）。

    与 :func:`render_assignment_context_from_contract` /
    func:`render_target_prompt_contract`（v2 主用）并存仅为兼容既有调用。
    """
    parts: list[str] = []
    parts.append(f"## WorkItem Context (v{contract.get('version', PROMPT_CONTRACT_VERSION)})")
    parts.append(f"ID: {contract.get('work_item_id', 'unknown')}")
    parts.append(f"Title: {contract.get('title', 'untitled')}")
    turn = contract.get("turn_type", "")
    if turn:
        parts.append(f"Turn Type: {turn}")
    phase = contract.get("phase", "")
    if phase:
        parts.append(f"Phase: {phase}")
    assignee = contract.get("assignee_role", "")
    if assignee:
        parts.append(f"Assignee: {assignee}")
    brief = contract.get("task_brief", "")
    if brief:
        parts.append(f"\nBrief: {brief}")
    parent = contract.get("parent_context", "")
    if parent:
        parts.append(f"\nParent Context: {parent}")
    deps = contract.get("dependencies", [])
    if deps:
        parts.append(f"\nDepends On: {', '.join(deps)}")
    output_fmt = contract.get("output_format", "")
    if output_fmt:
        parts.append(f"\nOutput Format: {output_fmt}")
    extra = contract.get("extra")
    if extra:
        rendered_extra = "\n".join(f"- {key}: {value}" for key, value in extra.items())
        parts.append(f"\nExtra:\n{rendered_extra}")
    return "\n".join(parts)


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """[deprecated] 旧式契约校验（v1 9 字段格式）。

    与 v2 API 并存仅为兼容既有调用；新代码请使用
    func:`has_prompt_contract` / :func:`normalize_prompt_contract`。
    """
    errors: list[str] = []
    if "work_item_id" not in contract or not contract["work_item_id"]:
        errors.append("Missing work_item_id")
    if "title" not in contract or not contract["title"]:
        errors.append("Missing title")
    if "turn_type" not in contract:
        errors.append("Missing turn_type")
    if "phase" not in contract or not contract["phase"]:
        errors.append("Missing phase")
    return errors


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def normalize_prompt_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                rendered = str(
                    item.get("value", "")
                    or item.get("input", "")
                    or item.get("raw", "")
                    or item.get("work_item_id", "")
                    or ""
                ).strip()
            else:
                rendered = "" if item is None else str(item).strip()
            if rendered:
                items.append(rendered)
        return items
    rendered = str(value).strip()
    return [rendered] if rendered else []


def normalize_dependency_specs(value: Any) -> list[Any]:
    """保留 dependency dict 结构（构建侧）。

    注意：与 ``normalize_prompt_text_list``（渲染侧，仅提取 value/input/raw/
    work_item_id 扁平为文本）刻意分工——构建需 target/type 等完整字段
    渲染只需展示文本。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [
            copy.deepcopy(item)
            for item in value
            if item is not None and (str(item).strip() or isinstance(item, dict))
        ]
    if isinstance(value, dict):
        return [copy.deepcopy(value)]
    rendered = str(value).strip()
    return [rendered] if rendered else []


def normalize_prompt_contract(value: Any) -> dict[str, Any]:
    payload = copy.deepcopy(value) if isinstance(value, dict) else {}
    assignment = _as_mapping(payload.get("assignment_context"))
    try:
        version = int(payload.get("version") or PROMPT_CONTRACT_VERSION)
    except (TypeError, ValueError):
        version = PROMPT_CONTRACT_VERSION
    normalized = {
        "version": version,
        "task_brief": str(payload.get("task_brief", "") or "").strip(),
        "assignment_context": {
            "upstream_intent_summary": str(
                assignment.get("upstream_intent_summary", "") or ""
            ).strip(),
            "manager_planning_handoff": str(
                assignment.get("manager_planning_handoff", "") or ""
            ).strip(),
            "manager_outcome_dispatch": _as_bool(assignment.get("manager_outcome_dispatch", False)),
            "owned_outcome_kind": str(assignment.get("owned_outcome_kind", "") or "execute").strip()
            or "execute",
            "scope_key": str(assignment.get("scope_key", "") or "").strip(),
            "deliverables": normalize_prompt_text_list(assignment.get("deliverables", [])),
            "acceptance_criteria": normalize_prompt_text_list(
                assignment.get("acceptance_criteria", [])
            ),
            "dependency_specs": normalize_dependency_specs(assignment.get("dependency_specs", [])),
            "coordination_notes": str(assignment.get("coordination_notes", "") or "").strip(),
            "delegation_rationale": str(assignment.get("delegation_rationale", "") or "").strip(),
            "non_overlap_guard": str(assignment.get("non_overlap_guard", "") or "").strip(),
        },
        "turn_profiles": copy.deepcopy(_as_mapping(payload.get("turn_profiles"))),
    }
    if payload.get("target_contract"):
        normalized["target_contract"] = normalize_prompt_contract(payload.get("target_contract"))
    source = _as_mapping(payload.get("source"))
    if source:
        normalized["source"] = copy.deepcopy(source)
    return normalized


def has_prompt_contract(value: Any) -> bool:
    contract = normalize_prompt_contract(value)
    return bool(contract.get("task_brief") or any(_assignment_has_content(contract)))


def _assignment_has_content(contract: dict[str, Any]) -> list[Any]:
    assignment = dict(contract.get("assignment_context", {}) or {})
    owned_outcome_kind = str(assignment.get("owned_outcome_kind", "") or "").strip()
    values: list[Any] = [
        assignment.get("upstream_intent_summary"),
        assignment.get("manager_planning_handoff"),
        assignment.get("manager_outcome_dispatch"),
        assignment.get("scope_key"),
        assignment.get("deliverables"),
        assignment.get("acceptance_criteria"),
        assignment.get("dependency_specs"),
        assignment.get("coordination_notes"),
        assignment.get("delegation_rationale"),
        assignment.get("non_overlap_guard"),
    ]
    if owned_outcome_kind and owned_outcome_kind != "execute":
        values.append(owned_outcome_kind)
    return values


def make_prompt_contract(
    *,
    task_brief: str,
    upstream_intent_summary: str = "",
    manager_planning_handoff: str = "",
    manager_outcome_dispatch: bool = False,
    owned_outcome_kind: str = "execute",
    scope_key: str = "",
    deliverables: Any = None,
    acceptance_criteria: Any = None,
    dependency_specs: Any = None,
    coordination_notes: str = "",
    delegation_rationale: str = "",
    non_overlap_guard: str = "",
    turn_profiles: dict[str, Any] | None = None,
    target_contract: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": PROMPT_CONTRACT_VERSION,
        "task_brief": str(task_brief or "").strip(),
        "assignment_context": {
            "upstream_intent_summary": str(upstream_intent_summary or "").strip(),
            "manager_planning_handoff": str(manager_planning_handoff or "").strip(),
            "manager_outcome_dispatch": _as_bool(manager_outcome_dispatch),
            "owned_outcome_kind": str(owned_outcome_kind or "execute").strip() or "execute",
            "scope_key": str(scope_key or "").strip(),
            "deliverables": normalize_prompt_text_list(deliverables),
            "acceptance_criteria": normalize_prompt_text_list(acceptance_criteria),
            "dependency_specs": normalize_dependency_specs(dependency_specs),
            "coordination_notes": str(coordination_notes or "").strip(),
            "delegation_rationale": str(delegation_rationale or "").strip(),
            "non_overlap_guard": str(non_overlap_guard or "").strip(),
        },
        "turn_profiles": dict(turn_profiles or {}),
    }
    if target_contract:
        payload["target_contract"] = normalize_prompt_contract(target_contract)
    if source:
        payload["source"] = dict(source)
    return normalize_prompt_contract(payload)


def make_prompt_contract_blocker(reason: str) -> dict[str, Any]:
    return make_prompt_contract(
        task_brief="SYSTEM BLOCKER: prompt_contract is missing or incomplete for this WorkItem.",
        deliverables=["Do not execute the original task until a valid prompt_contract exists."],
        acceptance_criteria=[str(reason or "Missing prompt contract.").strip()],
        source={"kind": "prompt_contract_blocker"},
    )


def prompt_contract_from_delegate_item(
    item: dict[str, Any],
    *,
    task_brief: str,
    upstream_intent_summary: str = "",
    manager_planning_handoff: str = "",
    manager_outcome_dispatch: bool = False,
    owned_outcome_kind: str = "execute",
    scope_key: str = "",
    dependency_specs: Any = None,
) -> dict[str, Any]:
    return make_prompt_contract(
        task_brief=task_brief,
        upstream_intent_summary=upstream_intent_summary,
        manager_planning_handoff=manager_planning_handoff,
        manager_outcome_dispatch=manager_outcome_dispatch,
        owned_outcome_kind=owned_outcome_kind,
        scope_key=scope_key,
        deliverables=item.get("deliverables") or item.get("outputs") or [],
        acceptance_criteria=item.get("acceptance_criteria") or item.get("done_when") or [],
        dependency_specs=dependency_specs,
        coordination_notes=str(item.get("coordination_notes", "") or "").strip(),
        delegation_rationale=str(item.get("delegation_rationale", "") or "").strip(),
        non_overlap_guard=str(item.get("non_overlap_guard", "") or "").strip(),
        source={"kind": "delegate_work"},
    )


def prompt_contract_from_work_item(
    work_item: Any,
    *,
    task_metadata: dict[str, Any] | None = None,
    task_description: str = "",
) -> dict[str, Any]:
    """从 WorkItem 派生 prompt 契约（优先级：已有契约 → legacy assignment → 派生）。

    数据源合并链（后者为回退）：legacy_assignment > work_item.metadata >
    task_metadata > delegation_playbook > 参数/属性。每一字段沿此链取首个非空值
    职责分离：playbook 只提供全局意图，assignment 提供字段级覆盖。
    """
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    task_metadata = dict(task_metadata or {})
    existing = metadata.get("prompt_contract") or task_metadata.get("prompt_contract")
    if has_prompt_contract(existing):
        return normalize_prompt_contract(existing)
    legacy_assignment = dict(
        metadata.get("prompt_assignment", {}) or task_metadata.get("prompt_assignment", {}) or {}
    )
    task_brief = str(
        legacy_assignment.get("task_brief", "")
        or legacy_assignment.get("primary_task_brief", "")
        or metadata.get("brief", "")
        or getattr(work_item, "summary", "")
        or task_description
        or getattr(work_item, "title", "")
        or ""
    ).strip()
    if not task_brief:
        return make_prompt_contract_blocker(
            f"WorkItem `{str(getattr(work_item, 'work_item_id', '') or '').strip()}` has no task_brief."
        )
    playbook = dict(
        metadata.get("delegation_playbook", {})
        or task_metadata.get("delegation_playbook", {})
        or {}
    )
    return make_prompt_contract(
        task_brief=task_brief,
        upstream_intent_summary=str(
            legacy_assignment.get("upstream_intent_summary", "")
            or metadata.get("upstream_intent_summary", "")
            or task_metadata.get("global_intent_summary", "")
            or playbook.get("global_intent_summary", "")
            or playbook.get("intent_summary", "")
            or ""
        ).strip(),
        manager_planning_handoff=str(
            legacy_assignment.get("manager_planning_handoff", "")
            or metadata.get("manager_planning_handoff", "")
            or metadata.get("planning_context", "")
            or ""
        ).strip(),
        manager_outcome_dispatch=_as_bool(
            legacy_assignment.get(
                "manager_outcome_dispatch", metadata.get("manager_outcome_dispatch", False)
            )
        ),
        owned_outcome_kind=str(
            legacy_assignment.get(
                "owned_outcome_kind",
                metadata.get("owned_outcome_kind", _work_item_kind(work_item)),
            )
            or "execute"
        ).strip(),
        scope_key=str(
            legacy_assignment.get("scope_key", metadata.get("scope_key", "")) or ""
        ).strip(),
        deliverables=legacy_assignment.get(
            "deliverables",
            legacy_assignment.get(
                "outputs", metadata.get("deliverables", metadata.get("outputs", []))
            ),
        ),
        acceptance_criteria=legacy_assignment.get(
            "acceptance_criteria",
            legacy_assignment.get(
                "done_when", metadata.get("acceptance_criteria", metadata.get("done_when", []))
            ),
        ),
        dependency_specs=legacy_assignment.get(
            "dependency_specs",
            legacy_assignment.get(
                "dependency_work_item_ids",
                metadata.get("dependency_specs", metadata.get("dependency_work_item_ids", [])),
            ),
        ),
        coordination_notes=str(
            legacy_assignment.get("coordination_notes", metadata.get("coordination_notes", ""))
            or ""
        ).strip(),
        delegation_rationale=str(
            legacy_assignment.get("delegation_rationale", metadata.get("delegation_rationale", ""))
            or ""
        ).strip(),
        non_overlap_guard=str(
            legacy_assignment.get("non_overlap_guard", metadata.get("non_overlap_guard", "")) or ""
        ).strip(),
        source={"kind": "normalized_legacy_work_item"},
    )


def _append_section(lines: list[str], title: str, value: Any) -> None:
    text = str(value or "").strip()
    if text:
        lines.extend(["", f"### {title}", text])


def render_assignment_context_from_contract(
    contract: dict[str, Any],
    *,
    include_dispatch_fields: bool = False,
) -> str:
    contract = normalize_prompt_contract(contract)
    assignment = dict(contract.get("assignment_context", {}) or {})
    lines: list[str] = ["## Work Item Assignment Context"]
    if include_dispatch_fields and assignment.get("manager_outcome_dispatch"):
        owned_kind = str(assignment.get("owned_outcome_kind") or "execute").strip()
        lines.append(
            "Manager outcome turn: you own the final outcome, but this turn "
            "starts with delegation to direct reports before local integration. "
            f"Owned outcome kind: {owned_kind}. Do not execute the production "
            "work yourself in this dispatch turn unless no downstream seat is a fit."
        )
    if assignment.get("upstream_intent_summary"):
        _append_section(lines, "Upstream Intent Summary", assignment["upstream_intent_summary"])
    if include_dispatch_fields and assignment.get("manager_planning_handoff"):
        _append_section(lines, "Manager Planning Handoff", assignment["manager_planning_handoff"])
    if assignment.get("scope_key"):
        _append_section(lines, "Scope Key", assignment["scope_key"])
    for title, key in (
        ("Deliverables", "deliverables"),
        ("Acceptance Criteria", "acceptance_criteria"),
        ("Dependencies", "dependency_specs"),
    ):
        items = normalize_prompt_text_list(assignment.get(key, []))
        if items:
            lines.extend(["", f"### {title}"])
            lines.extend(f"- {item}" for item in items)
    for title, key in (
        ("Coordination Notes", "coordination_notes"),
        ("Delegation Rationale", "delegation_rationale"),
        ("Boundaries / Non-overlap Guard", "non_overlap_guard"),
    ):
        _append_section(lines, title, assignment.get(key, ""))
    if len(lines) == 1:
        return ""
    return "\n".join(lines).strip()


def render_target_prompt_contract(
    contract: dict[str, Any], *, heading: str = "### Target Work Item Contract"
) -> str:
    contract = normalize_prompt_contract(contract)
    lines: list[str] = [heading]
    task_brief = str(contract.get("task_brief", "") or "").strip()
    if task_brief:
        lines.extend(["", "#### Task Brief", task_brief])
    assignment = render_assignment_context_from_contract(contract, include_dispatch_fields=True)
    if assignment:
        assignment = assignment.replace(
            "## Work Item Assignment Context", "#### Assignment Context", 1
        )
        lines.extend(["", assignment])
    if len(lines) == 1:
        return ""
    return "\n".join(lines).strip()


def is_report_prompt_turn(metadata: dict[str, Any] | None) -> bool:
    payload = dict(metadata or {})
    return bool(
        _as_bool(payload.get("report_execution_work_item", False))
        or str(payload.get("current_turn_mode", "") or "").strip() == "report_required"
        or str(payload.get("work_item_turn_type", "") or "").strip() == "report"
        or str(payload.get("work_kind", "") or "").strip() == "report"
    )
