from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bandi.company.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    OrgWorkItemRuntimeBlueprint,
    OrgWorkItemSeed,
    WorkItemDeliveryPolicy,
    WorkItemDependencySpec,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    WorkItemReviewPolicy,
    _projection_id,
    _role_review_owner,
    build_custom_org_work_item_blueprint,
    deserialize_company_work_item_plan,
    serialize_company_work_item_plan,
)


def _full_projection_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "projection_id": "corporate::execute::engineer::a-001",
        "turn_type": "execute",
        "role_id": "engineer",
        "title": "Engineer Execute",
        "summary": "do the thing",
        "dependency_projection_ids": ["corporate::intake::ceo"],
        "dependency_classes": {"corporate::intake::ceo": "hard"},
        "team_id": "team::eng",
        "seat_id": "seat::team::eng::engineer",
        "manager_role_id": "em",
        "manager_seat_id": "seat::team::eng::em",
        "execution_strategy": "auto",
        "preferred_external_agent": "ext-1",
        "parallel_group": "g1",
        "prompt_refs": ["p1"],
        "skill_refs": ["s1"],
        "handoff_template_ref": "ht",
        "memory_policy_ref": "mp",
        "artifact_contract_ref": "ac",
        "allowed_delegate_role_ids": ["analyst"],
        "contact_role_ids": ["ceo"],
        "gate_policy": {"type": "review", "reviewer_role": "em"},
        "review_policy": {"review_owner_role_id": "em", "review_level": "manager"},
        "delivery_policy": {"user_visible": True},
        "metadata": {"source": "test"},
    }
    payload.update(overrides)
    return payload


class TestWorkItemDependencySpec:
    def test_to_dict_roundtrip(self):
        spec = WorkItemDependencySpec(
            projection_id="a",
            dependency_projection_id="b",
            dependency_class="hard",
            metadata={"k": "v"},
        )
        restored = WorkItemDependencySpec.from_dict(spec.to_dict())
        assert restored == spec

    def test_from_dict_defaults(self):
        spec = WorkItemDependencySpec.from_dict(
            {"projection_id": " a ", "dependency_projection_id": " b "}
        )
        assert spec.projection_id == "a"
        assert spec.dependency_projection_id == "b"
        assert spec.dependency_class == "hard"
        assert spec.metadata == {}

    def test_from_dict_none_returns_empty_spec(self):
        spec = WorkItemDependencySpec.from_dict(None)
        assert spec.projection_id == ""
        assert spec.dependency_projection_id == ""
        assert spec.dependency_class == "hard"

    def test_from_dict_metadata_copy(self):
        source = {"projection_id": "a", "dependency_projection_id": "b", "metadata": {"k": "v"}}
        spec = WorkItemDependencySpec.from_dict(source)
        spec.metadata["k"] = "changed"
        assert source["metadata"]["k"] == "v"


class TestWorkItemGatePolicy:
    def test_to_dict_roundtrip(self):
        policy = WorkItemGatePolicy(
            gate_type="review",
            instructions="check",
            reviewer_role="em",
            requires_human=True,
            on_reject="rework",
            rework_projection_id="corporate::execute::engineer",
            max_retries=3,
            metadata={"source": "test"},
        )
        assert WorkItemGatePolicy.from_dict(policy.to_dict()) == policy

    def test_from_dict_none_returns_default_instance(self):
        policy = WorkItemGatePolicy.from_dict(None)
        assert policy == WorkItemGatePolicy()

    def test_default_instance_serializes_to_none(self):
        spec = WorkItemProjectionSpec.from_dict(_full_projection_dict())
        assert spec.gate_policy != WorkItemGatePolicy()
        default_spec = WorkItemProjectionSpec.from_dict(spec.to_dict())
        default_spec.gate_policy = WorkItemGatePolicy()
        assert default_spec.to_dict()["gate_policy"] is None

    def test_post_init_normalizes_enum_fields(self):
        policy = WorkItemGatePolicy(gate_type="REVIEW", on_reject="HALT")
        assert policy.gate_type == "review"
        assert policy.on_reject == "halt"

    def test_rework_projection_id_falls_back_to_metadata(self):
        policy = WorkItemGatePolicy(metadata={"rework_projection_id": "corporate::x::y"})
        assert policy.rework_projection_id == "corporate::x::y"
        assert policy.metadata["rework_projection_id"] == "corporate::x::y"

    def test_from_dict_reads_rework_from_top_or_metadata(self):
        top = WorkItemGatePolicy.from_dict(
            {"type": "review", "rework_projection_id": "corporate::a"}
        )
        assert top.rework_projection_id == "corporate::a"
        nested = WorkItemGatePolicy.from_dict(
            {"type": "review", "metadata": {"rework_projection_id": "corporate::b"}}
        )
        assert nested.rework_projection_id == "corporate::b"

    def test_from_dict_empty_dict_returns_default_instance(self):
        assert WorkItemGatePolicy.from_dict({}) == WorkItemGatePolicy()


class TestWorkItemReviewPolicy:
    def test_to_dict_roundtrip(self):
        policy = WorkItemReviewPolicy(
            review_owner_role_id="em",
            review_level="manager",
            max_reworks=5,
            metadata={"k": "v"},
        )
        assert WorkItemReviewPolicy.from_dict(policy.to_dict()) == policy

    def test_from_dict_defaults(self):
        policy = WorkItemReviewPolicy.from_dict({"review_owner_role_id": " em "})
        assert policy.review_owner_role_id == "em"
        assert policy.review_level == "manager"
        assert policy.max_reworks == 10

    def test_from_dict_none_returns_defaults(self):
        policy = WorkItemReviewPolicy.from_dict(None)
        assert policy.review_level == "manager"
        assert policy.max_reworks == 10


class TestWorkItemDeliveryPolicy:
    def test_to_dict_roundtrip(self):
        policy = WorkItemDeliveryPolicy(
            user_visible=True,
            authoritative_output=True,
            requires_user_feedback=True,
        )
        assert WorkItemDeliveryPolicy.from_dict(policy.to_dict()) == policy

    def test_from_dict_defaults_false(self):
        policy = WorkItemDeliveryPolicy.from_dict({})
        assert not policy.user_visible
        assert not policy.authoritative_output
        assert not policy.requires_user_feedback

    def test_from_dict_none_returns_defaults(self):
        policy = WorkItemDeliveryPolicy.from_dict(None)
        assert not policy.user_visible


class TestWorkItemProjectionSpec:
    def test_to_dict_roundtrip_full(self):
        spec = WorkItemProjectionSpec.from_dict(_full_projection_dict())
        restored = WorkItemProjectionSpec.from_dict(spec.to_dict())
        assert restored == spec

    def test_from_dict_minimal(self):
        spec = WorkItemProjectionSpec.from_dict(
            {"projection_id": "p1", "turn_type": "execute", "role_id": "engineer", "title": ""}
        )
        assert spec.turn_type == "execute"
        assert spec.role_id == "engineer"
        assert spec.title == "p1"
        assert spec.dependency_projection_ids == []
        assert spec.gate_policy == WorkItemGatePolicy()
        assert spec.review_policy.review_level == "manager"
        assert not spec.delivery_policy.user_visible

    def test_turn_type_normalized_to_enum_value(self):
        spec = WorkItemProjectionSpec.from_dict(
            {"projection_id": "p1", "turn_type": "EXEC", "role_id": "engineer"}
        )
        assert spec.turn_type == "execute"

    def test_turn_type_unknown_value_kept_lowercase(self):
        spec = WorkItemProjectionSpec.from_dict(
            {"projection_id": "p1", "turn_type": "custom_kind", "role_id": "engineer"}
        )
        assert spec.turn_type == "custom_kind"

    def test_dependency_classes_filters_blank_entries(self):
        spec = WorkItemProjectionSpec.from_dict(
            {
                "projection_id": "p1",
                "turn_type": "execute",
                "role_id": "engineer",
                "dependency_classes": {"a": "hard", "": "soft", "b": ""},
            }
        )
        assert spec.dependency_classes == {"a": "hard"}

    def test_nested_policies_parsed(self):
        spec = WorkItemProjectionSpec.from_dict(_full_projection_dict())
        assert spec.gate_policy is not None
        assert spec.gate_policy.reviewer_role == "em"
        assert spec.review_policy.review_owner_role_id == "em"
        assert spec.delivery_policy.user_visible

    def test_metadata_isolation(self):
        spec = WorkItemProjectionSpec.from_dict(_full_projection_dict())
        spec.metadata["new"] = "x"
        assert "new" not in _full_projection_dict()["metadata"]

    def test_preferred_external_agent_stripped(self):
        spec = WorkItemProjectionSpec.from_dict(
            {
                "projection_id": "p1",
                "turn_type": "execute",
                "role_id": "engineer",
                "preferred_external_agent": " ext-9 ",
            }
        )
        assert spec.preferred_external_agent == "ext-9"


class TestCompanyWorkItemRuntimePlan:
    def _plan(self) -> CompanyWorkItemRuntimePlan:
        return CompanyWorkItemRuntimePlan(
            profile="corporate",
            final_decider_role_id="ceo",
            top_level_role_ids=["ceo"],
            root_projection_id="corporate::intake::ceo",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="corporate::intake::ceo",
                    turn_type="intake",
                    role_id="ceo",
                    title="CEO Intake",
                ),
                WorkItemProjectionSpec(
                    projection_id="corporate::execute::engineer",
                    turn_type="execute",
                    role_id="engineer",
                    title="Engineer Execute",
                    dependency_projection_ids=["corporate::intake::ceo"],
                ),
            ],
            dependencies=[
                WorkItemDependencySpec(
                    projection_id="corporate::execute::engineer",
                    dependency_projection_id="corporate::intake::ceo",
                )
            ],
            collaboration_links=[{"source_role_id": "ceo", "target_role_id": "engineer"}],
            metadata={"source": "test"},
        )

    def test_to_dict_roundtrip(self):
        plan = self._plan()
        assert CompanyWorkItemRuntimePlan.from_dict(plan.to_dict()) == plan

    def test_from_dict_supports_seeds_alias(self):
        plan = CompanyWorkItemRuntimePlan.from_dict({"seeds": [_full_projection_dict()]})
        assert len(plan.projections) == 1
        assert plan.projections[0].projection_id == "corporate::execute::engineer::a-001"

    def test_projection_by_id(self):
        plan = self._plan()
        mapping = plan.projection_by_id()
        assert set(mapping) == {"corporate::intake::ceo", "corporate::execute::engineer"}
        assert mapping["corporate::execute::engineer"].role_id == "engineer"

    def test_projection_order_map(self):
        plan = self._plan()
        ordering = plan.projection_order_map()
        assert ordering["corporate::intake::ceo"] == 0
        assert ordering["corporate::execute::engineer"] == 1

    def test_dependencies_for(self):
        plan = self._plan()
        assert plan.dependencies_for("corporate::execute::engineer") == ["corporate::intake::ceo"]
        assert plan.dependencies_for("corporate::intake::ceo") == []
        assert plan.dependencies_for("missing") == []

    def test_dependent_projection_ids(self):
        plan = self._plan()
        assert plan.dependent_projection_ids("corporate::intake::ceo") == [
            "corporate::execute::engineer"
        ]
        assert plan.dependent_projection_ids("corporate::execute::engineer") == []
        assert plan.dependent_projection_ids("") == []


class TestSerialization:
    def test_serialize_none_returns_empty_dict(self):
        assert serialize_company_work_item_plan(None) == {}

    def test_serialize_deserialize_roundtrip(self):
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            final_decider_role_id="ceo",
            root_projection_id="corporate::intake::ceo",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="corporate::intake::ceo",
                    turn_type="intake",
                    role_id="ceo",
                    title="CEO Intake",
                )
            ],
        )
        restored = deserialize_company_work_item_plan(serialize_company_work_item_plan(plan))
        assert restored == plan

    def test_deserialize_none_returns_empty_plan(self):
        plan = deserialize_company_work_item_plan(None)
        assert plan.profile == "corporate"
        assert plan.projections == []

    def test_serialize_includes_runtime_markers(self):
        plan = CompanyWorkItemRuntimePlan(profile="corporate")
        data = serialize_company_work_item_plan(plan)
        assert data["runtime_model"] == "multi_team_org"
        assert data["work_item_driven"] is True


class TestOrgWorkItemSeed:
    def test_to_dict(self):
        seed = OrgWorkItemSeed(
            projection_id="custom::execute::engineer",
            turn_type="execute",
            role_id="engineer",
            team_id="team::eng",
            seat_id="seat::team::eng::engineer",
            manager_role_id="em",
            title="Engineer Execute",
            summary="do work",
            skill_refs=["s1"],
            prompt_refs=["p1"],
            allowed_delegate_role_ids=["analyst"],
            review_owner_role_id="em",
            dependency_work_item_ids=["custom::intake::ceo"],
            metadata={"source": "test"},
        )
        data = seed.to_dict()
        assert data["projection_id"] == "custom::execute::engineer"
        assert data["skill_refs"] == ["s1"]
        assert data["review_owner_role_id"] == "em"
        assert data["metadata"]["source"] == "test"

    def test_to_dict_list_copies(self):
        seed = OrgWorkItemSeed(
            projection_id="p",
            turn_type="execute",
            role_id="r",
            team_id="t",
            seat_id="s",
            manager_role_id="m",
            title="T",
            summary="S",
        )
        data = seed.to_dict()
        data["skill_refs"].append("mutated")
        assert seed.skill_refs == []

    def test_to_dict_defaults(self):
        seed = OrgWorkItemSeed(
            projection_id="p",
            turn_type="execute",
            role_id="r",
            team_id="t",
            seat_id="s",
            manager_role_id="m",
            title="T",
            summary="S",
        )
        data = seed.to_dict()
        assert data["allowed_delegate_role_ids"] == []
        assert data["dependency_work_item_ids"] == []
        assert data["review_owner_role_id"] == ""


class TestOrgWorkItemRuntimeBlueprint:
    def test_to_dict(self):
        blueprint = OrgWorkItemRuntimeBlueprint(
            final_decider_role_id="ceo",
            top_level_role_ids=["ceo"],
            root_projection_id="custom::intake::ceo",
            seeds=[
                OrgWorkItemSeed(
                    projection_id="custom::intake::ceo",
                    turn_type="intake",
                    role_id="ceo",
                    team_id="team::ceo",
                    seat_id="seat::team::ceo::ceo",
                    manager_role_id="",
                    title="CEO Intake",
                    summary="intake",
                )
            ],
            collaboration_links=[{"source_role_id": "ceo", "target_role_id": "engineer"}],
            metadata={"team_count": 1},
        )
        data = blueprint.to_dict()
        assert data["profile"] == "custom"
        assert data["runtime_model"] == "multi_team_org"
        assert data["work_item_driven"] is True
        assert len(data["seeds"]) == 1
        assert data["seeds"][0]["title"] == "CEO Intake"
        assert data["metadata"]["team_count"] == 1

    def test_defaults(self):
        blueprint = OrgWorkItemRuntimeBlueprint()
        assert blueprint.profile == "custom"
        assert blueprint.seeds == []
        assert blueprint.collaboration_links == []

    def test_from_dict_roundtrip(self):
        blueprint = OrgWorkItemRuntimeBlueprint(
            final_decider_role_id="ceo",
            top_level_role_ids=["ceo"],
            root_projection_id="custom::intake::ceo",
            seeds=[
                OrgWorkItemSeed(
                    projection_id="custom::intake::ceo",
                    turn_type="intake",
                    role_id="ceo",
                    team_id="team::ceo",
                    seat_id="seat::team::ceo::ceo",
                    manager_role_id="",
                    title="CEO Intake",
                    summary="intake",
                    skill_refs=["s1"],
                    allowed_delegate_role_ids=["engineer"],
                )
            ],
            collaboration_links=[{"source_role_id": "ceo", "target_role_id": "engineer"}],
            metadata={"team_count": 1},
        )
        assert OrgWorkItemRuntimeBlueprint.from_dict(blueprint.to_dict()) == blueprint

    def test_from_dict_empty_returns_defaults(self):
        blueprint = OrgWorkItemRuntimeBlueprint.from_dict(None)
        assert blueprint.profile == "custom"
        assert blueprint.seeds == []


class TestPlannerHelpers:
    def test_projection_id_joins_parts(self):
        assert _projection_id("custom", "intake", "ceo") == "custom::intake::ceo"

    def test_projection_id_skips_empty_parts(self):
        assert _projection_id("custom", "", "ceo") == "custom::ceo"

    def test_projection_id_replaces_spaces_with_underscores(self):
        assert _projection_id("team eng", "x") == "team_eng::x"

    def test_projection_id_strips_parts(self):
        assert _projection_id(" a ", " b ") == "a::b"

    def test_projection_id_all_empty_returns_empty(self):
        assert _projection_id("", "", "") == ""

    def test_review_owner_uses_runtime_policy_review_role(self):
        agent = _FakeAgent(runtime_policy={"review_role": "platform-review"})
        assert _role_review_owner(agent, "manager", "ceo") == "platform-review"

    def test_review_owner_falls_back_to_manager_role_id(self):
        agent = _FakeAgent(runtime_policy={"other": "x"})
        assert _role_review_owner(agent, "manager", "ceo") == "manager"

    def test_review_owner_falls_back_to_final_decider(self):
        agent = _FakeAgent(runtime_policy={})
        assert _role_review_owner(agent, "", "ceo") == "ceo"

    def test_review_owner_without_runtime_policy_falls_back(self):
        assert _role_review_owner(object(), "manager", "ceo") == "manager"

    def test_review_owner_with_none_policy_falls_back(self):
        agent = _FakeAgent(runtime_policy=None)
        assert _role_review_owner(agent, "manager", "ceo") == "manager"


class _FakeAgent:
    def __init__(self, runtime_policy: dict[str, str] | None) -> None:
        self.runtime_policy = runtime_policy


def _fake_agent(role_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        reports_to="",
        name=role_id,
        responsibility="",
        skill_refs=[],
        prompt_refs=[],
        runtime_policy={},
        preferred_external_agent="",
    )


class _FakeOrgEngine:
    def __init__(self) -> None:
        self._agents: dict[str, SimpleNamespace] = {}

    def get_agent(self, role_id: str) -> SimpleNamespace | None:
        return self._agents.get(role_id)


class TestBlueprintProjectionDedup:
    def test_projection_id_dedup_loops_until_unique(self):
        engine = _FakeOrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            engine,
            runtime_topology={
                "final_decider_role_id": "ceo",
                "seats": [
                    {"role_id": "r1", "seat_id": "s1", "team_id": "t1"},
                    {"role_id": "r1", "seat_id": "s1", "team_id": "t1"},
                    {"role_id": "r1", "seat_id": "s1", "team_id": "t1"},
                ],
            },
            original_request="build it",
        )
        ids = [seed.projection_id for seed in blueprint.seeds]
        assert len(ids) == len(set(ids)) == 4

    def test_different_roles_same_seat_id_unique(self):
        engine = _FakeOrgEngine()
        blueprint = build_custom_org_work_item_blueprint(
            engine,
            runtime_topology={
                "final_decider_role_id": "ceo",
                "seats": [
                    {"role_id": "r1", "seat_id": "s1", "team_id": "t1"},
                    {"role_id": "r2", "seat_id": "s1", "team_id": "t1"},
                ],
            },
            original_request="build it",
        )
        ids = [seed.projection_id for seed in blueprint.seeds]
        assert len(ids) == len(set(ids))


class TestBlueprintDelegateDependencies:
    def _build(self, seats: list[dict[str, Any]]) -> OrgWorkItemRuntimeBlueprint:
        engine = _FakeOrgEngine()
        return build_custom_org_work_item_blueprint(
            engine,
            runtime_topology={"final_decider_role_id": "ceo", "seats": seats},
            original_request="build it",
        )

    def test_delegated_children_encoded_as_dependencies(self):
        blueprint = self._build(
            [
                {
                    "role_id": "mgr",
                    "seat_id": "s-m",
                    "team_id": "t1",
                    "allowed_delegate_role_ids": ["dev"],
                },
                {"role_id": "dev", "seat_id": "s-d", "team_id": "t1"},
            ]
        )
        mgr = next(s for s in blueprint.seeds if s.role_id == "mgr")
        assert mgr.dependency_work_item_ids == ["custom::execute::s-d"]

    def test_no_delegates_leaves_dependencies_empty(self):
        blueprint = self._build([{"role_id": "dev", "seat_id": "s-d", "team_id": "t1"}])
        dev = next(s for s in blueprint.seeds if s.role_id == "dev")
        assert dev.dependency_work_item_ids == []


class TestDeserializationRobustness:
    def test_gate_policy_bad_int_falls_back(self):
        policy = WorkItemGatePolicy.from_dict(
            {"type": "review", "max_retries": "not-a-number", "requires_human": "false"}
        )
        assert policy.max_retries == 1
        assert policy.requires_human is False

    def test_review_policy_bad_int_falls_back(self):
        policy = WorkItemReviewPolicy.from_dict({"max_reworks": "NaN"})
        assert policy.max_reworks == 10

    def test_gate_policy_false_string_is_false(self):
        policy = WorkItemGatePolicy.from_dict({"requires_human": "false"})
        assert policy.requires_human is False
        truthy = WorkItemGatePolicy.from_dict({"requires_human": "1"})
        assert truthy.requires_human is True

    def test_delivery_policy_false_string_is_false(self):
        policy = WorkItemDeliveryPolicy.from_dict(
            {"user_visible": "false", "authoritative_output": "0", "requires_user_feedback": "no"}
        )
        assert policy.user_visible is False
        assert policy.authoritative_output is False
        assert policy.requires_user_feedback is False

    def test_zero_int_not_swallowed(self):
        policy = WorkItemGatePolicy.from_dict({"max_retries": 0})
        assert policy.max_retries == 0

    def test_projection_id_escapes_separator(self):
        assert _projection_id("custom", "a::b", "c d") == "custom::a_b::c_d"


class TestBuildIsTeamLeadDefense:
    def _build(self, seats: list[dict[str, Any]]) -> OrgWorkItemRuntimeBlueprint:
        engine = _FakeOrgEngine()
        return build_custom_org_work_item_blueprint(
            engine,
            runtime_topology={"final_decider_role_id": "ceo", "seats": seats},
            original_request="build it",
        )

    def test_is_team_lead_false_string_is_execute(self):
        blueprint = self._build(
            [
                {"role_id": "ceo", "seat_id": "s-c", "team_id": "t1"},
                {"role_id": "mgr", "seat_id": "s-m", "team_id": "t1", "is_team_lead": "false"},
            ]
        )
        mgr = next(s for s in blueprint.seeds if s.role_id == "mgr")
        assert mgr.turn_type == "execute"

    def test_is_team_lead_true_string_is_dispatch(self):
        blueprint = self._build(
            [
                {"role_id": "ceo", "seat_id": "s-c", "team_id": "t1"},
                {"role_id": "mgr", "seat_id": "s-m", "team_id": "t1", "is_team_lead": "1"},
            ]
        )
        mgr = next(s for s in blueprint.seeds if s.role_id == "mgr")
        assert mgr.turn_type == "dispatch"
