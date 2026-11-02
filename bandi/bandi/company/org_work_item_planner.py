# 功能：组织工作项规划构建——org 蓝图 → 公司运行时计划（数据模型见 work_item_plan_types.py）

"""Org-driven company work-item runtime planning ."""

from __future__ import annotations

from typing import Any

from bandi.company.work_item_plan_types import (
    _PROJECTION_SEP,
    CUSTOM_PROJECTION_PREFIX,
    TURN_TYPE_DISPATCH,
    TURN_TYPE_EXECUTE,
    TURN_TYPE_INTAKE,
    CompanyWorkItemRuntimePlan,
    OrgWorkItemRuntimeBlueprint,
    OrgWorkItemSeed,
    WorkItemDeliveryPolicy,
    WorkItemDependencySpec,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    WorkItemReviewPolicy,
    _as_bool,
    _clean_list,
    deserialize_company_work_item_plan,  # noqa: F401 - re-export
    serialize_company_work_item_plan,  # noqa: F401 - re-export
)


def _projection_id(*parts: str) -> str:
    # 功能：拼接投影 ID；将空格与投影分隔符全部归一为下划线，防 role_id/seat_id 污染投影结构（FG-13）
    return _PROJECTION_SEP.join(
        str(part or "").strip().replace(" ", "_").replace(_PROJECTION_SEP, "_")
        for part in parts
        if str(part or "").strip()
    )


def _role_review_owner(agent: Any, manager_role_id: str, final_decider_role_id: str) -> str:
    policy = getattr(agent, "runtime_policy", {}) or {}
    if isinstance(policy, dict):
        review_role = str(policy.get("review_role", "") or "").strip()
        if review_role:
            return review_role
    return manager_role_id or final_decider_role_id


def _downstream_role_ids(org_engine: Any, role_id: str) -> list[str]:
    if hasattr(org_engine, "get_allowed_downstream_roles"):
        return _clean_list(org_engine.get_allowed_downstream_roles(role_id))
    role = org_engine.get_role(role_id) if hasattr(org_engine, "get_role") else None
    return _clean_list(getattr(role, "can_spawn", []) if role is not None else [])


def build_custom_org_work_item_blueprint(
    org_engine: Any,
    *,
    runtime_topology: dict[str, Any],
    original_request: str = "",
    runtime_policy: dict[str, Any] | None = None,
) -> OrgWorkItemRuntimeBlueprint:
    """Build the custom runtime collaboration blueprint from org structure.

    This function deliberately consumes only org topology and runtime policy.
    Custom mode is org-first.
    """

    final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
    top_level_role_ids = _clean_list(runtime_topology.get("top_level_role_ids", []))
    if not final_decider_role_id:
        if len(top_level_role_ids) == 1:
            final_decider_role_id = top_level_role_ids[0]
        else:
            raise ValueError(
                "custom org runtime requires final_decider_role_id when multiple top-level roles exist"
            )

    seats = [
        dict(item)
        for item in list(runtime_topology.get("seats", []) or [])
        if isinstance(item, dict)
    ]
    teams = [
        dict(item)
        for item in list(runtime_topology.get("teams", []) or [])
        if isinstance(item, dict)
    ]
    team_by_id = {
        str(team.get("team_id", "") or "").strip(): team
        for team in teams
        if str(team.get("team_id", "") or "").strip()
    }
    seeds: list[OrgWorkItemSeed] = []
    links: list[dict[str, Any]] = []

    root_projection_id = _projection_id(
        CUSTOM_PROJECTION_PREFIX, TURN_TYPE_INTAKE, final_decider_role_id
    )
    seen_projection_ids: set[str] = set()

    for seat in seats:
        role_id = str(seat.get("role_id", "") or "").strip()
        seat_id = str(seat.get("seat_id", "") or "").strip()
        team_id = str(seat.get("team_id", "") or "").strip()
        if not role_id or not seat_id:
            continue
        agent = org_engine.get_agent(role_id) if hasattr(org_engine, "get_agent") else None
        manager_role_id = str(
            seat.get("manager_role_id", "") or getattr(agent, "reports_to", "") or ""
        ).strip()
        if manager_role_id == "owner":
            manager_role_id = ""
        allowed_delegate_role_ids = _clean_list(
            seat.get("allowed_delegate_role_ids") or _downstream_role_ids(org_engine, role_id)
        )
        contact_role_ids = _clean_list(seat.get("contact_role_ids", []))
        is_final_decider = role_id == final_decider_role_id
        is_manager = bool(
            allowed_delegate_role_ids
            or str(seat.get("managed_team_id", "") or "").strip()
            or _as_bool(seat.get("is_team_lead"))
        )
        turn_type = (
            TURN_TYPE_INTAKE
            if is_final_decider
            else (TURN_TYPE_DISPATCH if is_manager else TURN_TYPE_EXECUTE)
        )
        projection_id = (
            root_projection_id
            if is_final_decider
            else _projection_id(CUSTOM_PROJECTION_PREFIX, turn_type, seat_id)
        )
        if projection_id in seen_projection_ids:
            candidate = projection_id
            while candidate in seen_projection_ids:
                candidate = _projection_id(candidate, role_id)
            projection_id = candidate
        seen_projection_ids.add(projection_id)
        role_name = str(getattr(agent, "name", "") or role_id).strip()
        responsibility = str(getattr(agent, "responsibility", "") or "").strip()
        seeds.append(
            OrgWorkItemSeed(
                projection_id=projection_id,
                turn_type=turn_type,
                role_id=role_id,
                team_id=team_id,
                seat_id=seat_id,
                manager_role_id=manager_role_id,
                title=f"{role_name} {turn_type.title()}",
                summary=responsibility or original_request or f"{role_name} work item",
                skill_refs=list(getattr(agent, "skill_refs", []) or []),
                prompt_refs=list(getattr(agent, "prompt_refs", []) or []),
                allowed_delegate_role_ids=allowed_delegate_role_ids,
                review_owner_role_id=_role_review_owner(
                    agent, manager_role_id, final_decider_role_id
                ),
                metadata={
                    "source": "custom_org_work_item_runtime",
                    "role_name": role_name,
                    "responsibility": responsibility,
                    "team_id": team_id,
                    "team_name": str(
                        (team_by_id.get(team_id, {}) or {}).get("metadata", {}).get("lead_name", "")
                        or ""
                    ),
                    "contact_role_ids": contact_role_ids,
                    "managed_team_id": str(seat.get("managed_team_id", "") or "").strip(),
                    "preferred_external_agent": str(
                        seat.get("preferred_external_agent", "")
                        or getattr(agent, "preferred_external_agent", "")
                        or ""
                    ).strip(),
                    "selected_execution_agent": str(
                        seat.get("selected_execution_agent", "") or ""
                    ).strip(),
                    "runtime_policy": dict(getattr(agent, "runtime_policy", {}) or {}),
                },
            )
        )
        for delegate_role_id in allowed_delegate_role_ids:
            links.append(
                {
                    "source_role_id": role_id,
                    "target_role_id": delegate_role_id,
                    "link_type": "delegates_to",
                    "source_projection_id": projection_id,
                }
            )
        if manager_role_id:
            links.append(
                {
                    "source_role_id": role_id,
                    "target_role_id": manager_role_id,
                    "link_type": "reports_to",
                    "source_projection_id": projection_id,
                }
            )

    seed_projection_ids = {seed.projection_id for seed in seeds}
    if root_projection_id not in seed_projection_ids:
        final_agent = (
            org_engine.get_agent(final_decider_role_id)
            if hasattr(org_engine, "get_agent")
            else None
        )
        seeds.insert(
            0,
            OrgWorkItemSeed(
                projection_id=root_projection_id,
                turn_type=TURN_TYPE_INTAKE,
                role_id=final_decider_role_id,
                team_id=_projection_id("team", final_decider_role_id),
                seat_id=_projection_id(
                    "seat", "team", final_decider_role_id, final_decider_role_id
                ),
                manager_role_id="",
                title=f"{getattr(final_agent, 'name', final_decider_role_id)} Intake",
                summary=original_request or "Custom organization intake",
                skill_refs=list(getattr(final_agent, "skill_refs", []) or []),
                prompt_refs=list(getattr(final_agent, "prompt_refs", []) or []),
                allowed_delegate_role_ids=_downstream_role_ids(org_engine, final_decider_role_id),
                review_owner_role_id=final_decider_role_id,
                metadata={"source": "custom_org_work_item_runtime", "fallback_root": True},
            ),
        )

    role_to_projection = {seed.role_id: seed.projection_id for seed in seeds}
    for seed in seeds:
        delegated = [
            role_to_projection[delegate_role_id]
            for delegate_role_id in seed.allowed_delegate_role_ids
            if delegate_role_id in role_to_projection
        ]
        if delegated:
            seed.dependency_work_item_ids = list(dict.fromkeys(delegated))

    return OrgWorkItemRuntimeBlueprint(
        final_decider_role_id=final_decider_role_id,
        top_level_role_ids=top_level_role_ids,
        root_projection_id=root_projection_id,
        seeds=seeds,
        collaboration_links=links,
        metadata={
            "source": "custom_org_work_item_runtime",
            "team_count": len(teams),
            "seat_count": len(seats),
            "runtime_policy": dict(runtime_policy or {}),
        },
    )


def build_company_work_item_runtime_plan(
    org_engine: Any,
    *,
    profile: str = "corporate",
    runtime_topology: dict[str, Any],
    original_request: str = "",
    runtime_policy: dict[str, Any] | None = None,
) -> CompanyWorkItemRuntimePlan:
    """Build the company runtime plan from org topology, not a fixed step list."""

    normalized_profile = str(profile or "corporate").strip() or "corporate"
    blueprint = build_custom_org_work_item_blueprint(
        org_engine,
        runtime_topology=runtime_topology,
        original_request=original_request,
        runtime_policy=runtime_policy,
    )
    prefix_marker = f"{CUSTOM_PROJECTION_PREFIX}::"
    root_projection_id = blueprint.root_projection_id.replace(
        prefix_marker, f"{normalized_profile}::", 1
    )
    projections: list[WorkItemProjectionSpec] = []
    dependencies: list[WorkItemDependencySpec] = []
    for seed in blueprint.seeds:
        projection_id = seed.projection_id.replace(prefix_marker, f"{normalized_profile}::", 1)
        dependency_projection_ids = [
            item.replace(prefix_marker, f"{normalized_profile}::", 1)
            for item in list(seed.dependency_work_item_ids or [])
        ]
        if (
            projection_id != root_projection_id
            and root_projection_id
            and root_projection_id not in dependency_projection_ids
        ):
            dependency_projection_ids.insert(0, root_projection_id)
        for dependency_projection_id in dependency_projection_ids:
            dependencies.append(
                WorkItemDependencySpec(
                    projection_id=projection_id,
                    dependency_projection_id=dependency_projection_id,
                    dependency_class="hard",
                )
            )
        seed_metadata = dict(seed.metadata or {})
        metadata = {
            **seed_metadata,
            "source": "company_work_item_runtime_plan",
            "seed_source": seed_metadata.get("source", ""),
            "work_kind": seed.turn_type,
            "delegation_turn_kind": seed.turn_type,
            "allowed_delegate_role_ids": list(seed.allowed_delegate_role_ids),
            "dependency_projection_ids": list(dependency_projection_ids),
            "runtime_policy": dict(runtime_policy or {}),
        }
        is_root = projection_id == root_projection_id
        projections.append(
            WorkItemProjectionSpec(
                projection_id=projection_id,
                turn_type=seed.turn_type,
                role_id=seed.role_id,
                title=seed.title,
                summary=seed.summary,
                dependency_projection_ids=dependency_projection_ids,
                team_id=seed.team_id,
                seat_id=seed.seat_id,
                manager_role_id=seed.manager_role_id,
                prompt_refs=list(seed.prompt_refs),
                skill_refs=list(seed.skill_refs),
                allowed_delegate_role_ids=list(seed.allowed_delegate_role_ids),
                contact_role_ids=_clean_list(seed.metadata.get("contact_role_ids", [])),
                preferred_external_agent=str(
                    seed.metadata.get("preferred_external_agent", "") or ""
                ).strip()
                or None,
                gate_policy=(
                    WorkItemGatePolicy(
                        gate_type="review",
                        reviewer_role=seed.review_owner_role_id or None,
                        on_reject="rework",
                        rework_projection_id=projection_id,
                        metadata={"source": "work_item_review_policy"},
                    )
                    if seed.review_owner_role_id and not is_root
                    else WorkItemGatePolicy()
                ),
                review_policy=WorkItemReviewPolicy(
                    review_owner_role_id=seed.review_owner_role_id,
                    review_level="manager" if seed.review_owner_role_id else "human",
                ),
                delivery_policy=WorkItemDeliveryPolicy(
                    user_visible=is_root,
                    authoritative_output=is_root,
                    requires_user_feedback=is_root,
                ),
                metadata=metadata,
            )
        )

    return CompanyWorkItemRuntimePlan(
        profile=normalized_profile,
        final_decider_role_id=blueprint.final_decider_role_id,
        top_level_role_ids=list(blueprint.top_level_role_ids),
        root_projection_id=root_projection_id,
        projections=projections,
        dependencies=dependencies,
        collaboration_links=[dict(link) for link in blueprint.collaboration_links],
        metadata={
            **dict(blueprint.metadata or {}),
            "source": "company_work_item_runtime_plan",
            "runtime_model": "multi_team_org",
            "work_item_driven": True,
            "runtime_policy": dict(runtime_policy or {}),
        },
    )
