from __future__ import annotations

from typing import Any

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.company.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemDeliveryPolicy,
    WorkItemDependencySpec,
    WorkItemGatePolicy,
    WorkItemReviewPolicy,
    build_company_work_item_runtime_plan,
    build_custom_org_work_item_blueprint,
    deserialize_company_work_item_plan,
    serialize_company_work_item_plan,
)


def _seat(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role_id": "engineer",
        "seat_id": "seat::team::engineer::engineer",
        "team_id": "team::engineer",
        "manager_role_id": "em",
    }
    payload.update(overrides)
    return payload


class TestBuildCustomBlueprint:
    def test_single_top_level_derives_final_decider(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="ceo", seat_id="seat::team::ceo::ceo", manager_role_id="")],
            },
        )
        assert blueprint.final_decider_role_id == "ceo"
        assert blueprint.root_projection_id == "custom::intake::ceo"

    def test_multiple_top_level_without_final_decider_raises(self):
        org = OrgEngine()
        with pytest.raises(ValueError):
            build_custom_org_work_item_blueprint(
                org,
                runtime_topology={"top_level_role_ids": ["ceo", "cfo"], "seats": []},
            )

    def test_explicit_final_decider_with_multiple_top_level(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "final_decider_role_id": "cfo",
                "top_level_role_ids": ["ceo", "cfo"],
                "seats": [],
            },
        )
        assert blueprint.final_decider_role_id == "cfo"

    def test_seat_missing_role_or_seat_id_is_skipped(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="", seat_id=""), {"role_id": "ceo", "seat_id": "ceo-seat"}],
            },
        )
        assert {seed.role_id for seed in blueprint.seeds} == {"ceo"}

    def test_delegate_role_ids_fallback_to_can_spawn(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="em", seat_id="em-seat", manager_role_id="cto")],
            },
        )
        em_seed = next(s for s in blueprint.seeds if s.role_id == "em")
        assert em_seed.allowed_delegate_role_ids == ["engineer"]
        assert em_seed.turn_type == "dispatch"

    def test_seat_allowed_delegate_role_ids_takes_precedence(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [
                    _seat(
                        role_id="em",
                        seat_id="em-seat",
                        manager_role_id="cto",
                        allowed_delegate_role_ids=["architect"],
                    )
                ],
            },
        )
        em_seed = next(s for s in blueprint.seeds if s.role_id == "em")
        assert em_seed.allowed_delegate_role_ids == ["architect"]

    def test_manager_falls_back_to_reports_to(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="engineer", seat_id="eng-seat", manager_role_id="")],
            },
        )
        engineer_seed = next(s for s in blueprint.seeds if s.role_id == "engineer")
        assert engineer_seed.manager_role_id == "em"

    def test_owner_manager_normalized_to_empty(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="ceo", seat_id="ceo-seat", manager_role_id="owner")],
            },
        )
        ceo_seed = next(s for s in blueprint.seeds if s.role_id == "ceo")
        assert ceo_seed.manager_role_id == ""

    def test_final_decider_gets_intake_turn_type(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="ceo", seat_id="ceo-seat")],
            },
        )
        ceo_seed = next(s for s in blueprint.seeds if s.role_id == "ceo")
        assert ceo_seed.turn_type == "intake"
        assert ceo_seed.projection_id == "custom::intake::ceo"

    def test_projection_id_collision_appends_role_id(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [
                    _seat(role_id="ceo", seat_id="ceo-seat"),
                    _seat(role_id="ceo", seat_id="ceo-seat"),
                ],
            },
        )
        assert len({seed.projection_id for seed in blueprint.seeds}) == len(blueprint.seeds)

    def test_root_seed_inserted_when_missing(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="engineer", seat_id="eng-seat")],
            },
        )
        assert blueprint.seeds[0].projection_id == "custom::intake::ceo"
        assert blueprint.seeds[0].turn_type == "intake"
        assert blueprint.seeds[0].metadata.get("fallback_root") is True

    def test_collaboration_links_include_delegates_and_reports(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="em", seat_id="em-seat", manager_role_id="cto")],
            },
        )
        link_types = {
            (link["source_role_id"], link["target_role_id"], link["link_type"])
            for link in blueprint.collaboration_links
        }
        assert ("em", "engineer", "delegates_to") in link_types
        assert ("em", "cto", "reports_to") in link_types

    def test_agent_attribute_defaults_when_missing(self):
        class BareEngine:
            def get_agent(self, role_id: str) -> object:
                return object()

        blueprint = build_custom_org_work_item_blueprint(
            BareEngine(),
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="ceo", seat_id="ceo-seat")],
            },
        )
        ceo_seed = next(s for s in blueprint.seeds if s.role_id == "ceo")
        assert ceo_seed.skill_refs == []
        assert ceo_seed.prompt_refs == []
        assert ceo_seed.summary

    def test_blueprint_metadata_counts(self):
        org = OrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            org,
            runtime_topology={
                "top_level_role_ids": ["ceo"],
                "teams": [{"team_id": "team::eng"}],
                "seats": [_seat(role_id="engineer", seat_id="eng-seat")],
            },
        )
        assert blueprint.metadata["team_count"] == 1
        assert blueprint.metadata["seat_count"] == 1
        assert blueprint.metadata["source"] == "custom_org_work_item_runtime"


class TestBuildCompanyRuntimePlan:
    def _plan(self, **topology: Any) -> CompanyWorkItemRuntimePlan:
        return build_company_work_item_runtime_plan(
            OrgEngine(),
            runtime_topology={
                "final_decider_role_id": "ceo",
                "top_level_role_ids": ["ceo"],
                "seats": [_seat(role_id="ceo", seat_id="ceo-seat")],
                **topology,
            },
        )

    def test_profile_prefix_replacement(self):
        plan = self._plan()
        assert plan.profile == "corporate"
        assert plan.root_projection_id == "corporate::intake::ceo"
        assert all(p.projection_id.startswith("corporate::") for p in plan.projections)

    def test_root_dependency_injected_before_non_root(self):
        plan = self._plan(
            seats=[
                _seat(role_id="ceo", seat_id="ceo-seat"),
                _seat(role_id="engineer", seat_id="eng-seat"),
            ]
        )
        engineer = plan.projection_by_id()["corporate::execute::eng-seat"]
        assert engineer.dependency_projection_ids == ["corporate::intake::ceo"]

    def test_gate_policy_present_for_non_root_with_review_owner(self):
        plan = self._plan(seats=[_seat(role_id="engineer", seat_id="eng-seat")])
        engineer = plan.projection_by_id()["corporate::execute::eng-seat"]
        assert isinstance(engineer.gate_policy, WorkItemGatePolicy)
        assert engineer.gate_policy.gate_type == "review"
        assert engineer.gate_policy.reviewer_role == "em"
        assert engineer.gate_policy.on_reject == "rework"
        assert engineer.gate_policy.rework_projection_id == "corporate::execute::eng-seat"

    def test_gate_policy_absent_for_root(self):
        plan = self._plan()
        root = plan.projection_by_id()["corporate::intake::ceo"]
        assert root.gate_policy == WorkItemGatePolicy()
        assert root.to_dict()["gate_policy"] is None

    def test_review_policy_owner_falls_back_to_final_decider(self):
        plan = self._plan(
            seats=[
                _seat(role_id="ceo", seat_id="ceo-seat", manager_role_id=""),
                _seat(role_id="engineer", seat_id="eng-seat"),
            ]
        )
        root = plan.projection_by_id()["corporate::intake::ceo"]
        engineer = plan.projection_by_id()["corporate::execute::eng-seat"]
        assert isinstance(root.review_policy, WorkItemReviewPolicy)
        assert root.review_policy.review_owner_role_id == "ceo"
        assert engineer.review_policy.review_owner_role_id == "em"
        assert root.review_policy.review_level == "manager"
        assert engineer.review_policy.review_level == "manager"

    def test_delivery_policy_root_only_visible(self):
        plan = self._plan(
            seats=[
                _seat(role_id="ceo", seat_id="ceo-seat"),
                _seat(role_id="engineer", seat_id="eng-seat"),
            ]
        )
        root = plan.projection_by_id()["corporate::intake::ceo"]
        engineer = plan.projection_by_id()["corporate::execute::eng-seat"]
        assert isinstance(root.delivery_policy, WorkItemDeliveryPolicy)
        assert root.delivery_policy.user_visible is True
        assert engineer.delivery_policy.user_visible is False

    def test_metadata_contains_plan_markers(self):
        plan = self._plan()
        projection = plan.projections[0]
        assert projection.metadata["source"] == "company_work_item_runtime_plan"
        assert projection.metadata["work_kind"] == "intake"
        assert projection.metadata["delegation_turn_kind"] == "intake"
        assert plan.metadata["runtime_model"] == "multi_team_org"
        assert plan.metadata["work_item_driven"] is True

    def test_roundtrip_serialization(self):
        plan = self._plan(seats=[_seat(role_id="engineer", seat_id="eng-seat")])
        restored = deserialize_company_work_item_plan(serialize_company_work_item_plan(plan))
        assert restored == plan

    def test_empty_profile_defaults_to_corporate(self):
        plan = build_company_work_item_runtime_plan(
            OrgEngine(),
            profile="",
            runtime_topology={"top_level_role_ids": ["ceo"], "seats": []},
        )
        assert plan.profile == "corporate"

    def test_empty_seats_produce_only_root_projection(self):
        plan = self._plan()
        assert len(plan.projections) == 1
        assert plan.projections[0].projection_id == "corporate::intake::ceo"

    def test_dependencies_recorded_as_specs(self):
        plan = self._plan(seats=[_seat(role_id="engineer", seat_id="eng-seat")])
        assert plan.dependencies
        assert isinstance(plan.dependencies[0], WorkItemDependencySpec)
        assert plan.dependencies[0].dependency_class == "hard"
