from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bandi.company.work_item_identity import (
    GATE_REWORK_PROJECTION_ID_KEY,
    GATE_TARGET_PROJECTION_ID_KEY,
    RESULT_DELIVERY_ID_KEY,
    SOURCE_TASK_ID_KEY,
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    WorkItemTurnType,
    canonical_result_turn_id_for_task,
    canonical_turn_type_for_work_item,
    canonical_work_item_turn_type_for_kind,
    gate_rework_payload,
    is_delivery_turn,
    is_manager_reviewable_turn,
    mark_gate_rework_projection,
    mark_projected_work_item_task,
    mark_work_item_projection,
    migrate_work_item_projection_metadata,
    normalize_work_item_turn_type,
    projection_id_for_task,
    projection_id_for_work_item,
    result_delivery_id_for_task,
    result_delivery_identity_payload_for_task,
    rework_projection_id_for_gate,
    target_projection_id_for_decision,
    target_projection_ids_for_decision,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_identity_payload_from_metadata,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)


@dataclass
class FakeTask:
    id: str = "task-1"
    metadata: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class FakeWorkItem:
    projection_id: str = ""
    work_item_id: str = ""
    kind: str = ""
    metadata: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class FakeGate:
    metadata: dict[str, Any] = None
    rework_projection_id: str = ""

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class FakeDecision:
    target_projection_id: str = ""
    target_projection_ids: list[str] = None

    def __post_init__(self) -> None:
        if self.target_projection_ids is None:
            self.target_projection_ids = []


class TestNormalizeWorkItemTurnType:
    def test_valid_value_and_case_insensitive(self):
        assert normalize_work_item_turn_type("execute") == "execute"
        assert normalize_work_item_turn_type("EXECUTE") == "execute"

    def test_alias_delegate_and_delivery(self):
        assert normalize_work_item_turn_type("delegate") == "dispatch"
        assert normalize_work_item_turn_type("delivery") == "deliver"

    def test_alias_follow_up(self):
        assert normalize_work_item_turn_type("follow-up") == "followup"
        assert normalize_work_item_turn_type("follow_up") == "followup"

    def test_alias_synthesis(self):
        assert normalize_work_item_turn_type("synthesis") == "aggregate"

    def test_alias_self_evolution(self):
        assert normalize_work_item_turn_type("self-evolution") == "self_evolution"
        assert normalize_work_item_turn_type("self evolution") == "self_evolution"

    def test_fallbacks_used_and_normalized(self):
        assert normalize_work_item_turn_type("", fallback="plan") == "plan"
        assert normalize_work_item_turn_type("", fallback="Delivery") == "deliver"

    def test_unknown_passes_through_and_none(self):
        assert normalize_work_item_turn_type("custom_kind") == "custom_kind"
        assert normalize_work_item_turn_type(None) == ""


class TestCanonicalTurnTypeForKind:
    def test_maps_canonical_and_alias(self):
        assert canonical_work_item_turn_type_for_kind("review") == "review"
        assert canonical_work_item_turn_type_for_kind("delegate") == "dispatch"

    def test_fallback_used_and_empty_when_not_canonical(self):
        assert canonical_work_item_turn_type_for_kind("bogus", fallback="execute") == "execute"
        assert canonical_work_item_turn_type_for_kind("bogus", fallback="bogus2") == ""


class TestWorkItemProjectionIdFromMetadata:
    def test_reads_projection_key_and_blank_default(self):
        meta = {WORK_ITEM_PROJECTION_ID_KEY: "proj-1"}
        assert work_item_projection_id_from_metadata(meta) == "proj-1"
        assert work_item_projection_id_from_metadata({WORK_ITEM_PROJECTION_ID_KEY: "  "}) == ""

    def test_missing_and_empty_metadata_use_fallback(self):
        assert work_item_projection_id_from_metadata({}, fallback="fb") == "fb"
        assert work_item_projection_id_from_metadata(None, fallback="fb") == "fb"


class TestWorkItemTurnTypeFromMetadata:
    def test_reads_turn_type_key(self):
        meta = {WORK_ITEM_TURN_TYPE_KEY: "plan"}
        assert work_item_turn_type_from_metadata(meta) == "plan"

    def test_falls_back_to_work_kind_and_delegation_kind(self):
        assert work_item_turn_type_from_metadata({"work_kind": "delegate"}) == "dispatch"
        assert work_item_turn_type_from_metadata({"delegation_turn_kind": "review"}) == "review"

    def test_default_and_custom_fallback(self):
        assert work_item_turn_type_from_metadata(None) == "execute"
        assert work_item_turn_type_from_metadata({}, fallback="deliver") == "deliver"


class TestProjectionForTask:
    def test_projection_from_metadata_and_task_id_fallback(self):
        task = FakeTask(metadata={WORK_ITEM_PROJECTION_ID_KEY: "proj-t"})
        assert projection_id_for_task(task) == "proj-t"
        fallback_task = FakeTask(id="task-9")
        assert projection_id_for_task(fallback_task) == "task-9"

    def test_turn_type_from_metadata_and_default(self):
        task = FakeTask(metadata={WORK_ITEM_TURN_TYPE_KEY: "setup"})
        assert turn_type_for_task(task) == "setup"
        assert turn_type_for_task(FakeTask()) == "execute"


class TestProjectionForWorkItem:
    def test_projection_explicit_and_work_item_id_fallback(self):
        item = FakeWorkItem(projection_id="proj-explicit")
        assert projection_id_for_work_item(item) == "proj-explicit"
        fallback_item = FakeWorkItem(work_item_id="wi-7")
        assert projection_id_for_work_item(fallback_item) == "wi-7"

    def test_turn_type_from_kind_and_canonical_mapping(self):
        item = FakeWorkItem(kind="delegate")
        assert turn_type_for_work_item(item) == "dispatch"
        canonical_item = FakeWorkItem(kind="execute")
        assert canonical_turn_type_for_work_item(canonical_item) == "execute"
        assert canonical_turn_type_for_work_item({WORK_ITEM_TURN_TYPE_KEY: "plan"}) == "plan"


class TestDeliveryAndReviewable:
    def test_delivery_turn_and_alias(self):
        assert is_delivery_turn({WORK_ITEM_TURN_TYPE_KEY: "deliver"}) is True
        assert is_delivery_turn(FakeWorkItem(kind="delivery")) is True

    def test_execute_not_delivery(self):
        assert is_delivery_turn("execute") is False

    def test_manager_reviewable_execute_and_review(self):
        assert is_manager_reviewable_turn({WORK_ITEM_TURN_TYPE_KEY: "execute"}) is True
        assert is_manager_reviewable_turn("review") is True

    def test_dispatch_and_empty_not_reviewable(self):
        assert is_manager_reviewable_turn("dispatch") is False
        assert is_manager_reviewable_turn("") is False

    def test_non_manager_review_turn_types_derived_from_enum(self):
        from bandi.company.work_item import WorkItemTurnType
        from bandi.company.work_item_identity import _NON_MANAGER_REVIEW_TURN_TYPES

        expected = {"intake", "plan", "dispatch", "aggregate", "deliver", "self_evolution"}
        assert _NON_MANAGER_REVIEW_TURN_TYPES == expected
        assert _NON_MANAGER_REVIEW_TURN_TYPES <= frozenset(t.value for t in WorkItemTurnType)


class TestMarkWorkItemProjection:
    def test_sets_both_keys_and_normalizes_turn_type(self):
        result = mark_work_item_projection({}, projection_id="p-1", turn_type="plan")
        assert result[WORK_ITEM_PROJECTION_ID_KEY] == "p-1"
        assert result[WORK_ITEM_TURN_TYPE_KEY] == "plan"
        alias_result = mark_work_item_projection({}, turn_type="Delivery")
        assert alias_result[WORK_ITEM_TURN_TYPE_KEY] == "deliver"

    def test_preserves_existing_reads_existing_and_skips_blank(self):
        result = mark_work_item_projection({"other": 1}, projection_id="p-1")
        assert result["other"] == 1
        existing = mark_work_item_projection({WORK_ITEM_PROJECTION_ID_KEY: "p-x"})
        assert existing[WORK_ITEM_PROJECTION_ID_KEY] == "p-x"
        blank = mark_work_item_projection({}, projection_id="  ")
        assert WORK_ITEM_PROJECTION_ID_KEY not in blank

    def test_mark_projected_work_item_task(self):
        result = mark_projected_work_item_task({}, projection_id="p-2", turn_type="setup")
        assert result[WORK_ITEM_PROJECTION_ID_KEY] == "p-2"
        assert result[WORK_ITEM_TURN_TYPE_KEY] == "setup"


class TestWorkItemIdentityPayload:
    def test_explicit_values_and_empty_defaults(self):
        payload = work_item_identity_payload(projection_id="p-1", turn_type="plan")
        assert payload == {WORK_ITEM_PROJECTION_ID_KEY: "p-1", WORK_ITEM_TURN_TYPE_KEY: "plan"}
        assert work_item_identity_payload() == {}
        empty = work_item_identity_payload(include_empty=True)
        assert empty == {WORK_ITEM_PROJECTION_ID_KEY: "", WORK_ITEM_TURN_TYPE_KEY: ""}

    def test_source_fallback_and_explicit_override(self):
        source = {WORK_ITEM_PROJECTION_ID_KEY: "p-src", WORK_ITEM_TURN_TYPE_KEY: "review"}
        payload = work_item_identity_payload(source=source)
        assert payload == {WORK_ITEM_PROJECTION_ID_KEY: "p-src", WORK_ITEM_TURN_TYPE_KEY: "review"}
        overridden = work_item_identity_payload(projection_id="p-explicit", source=source)
        assert overridden[WORK_ITEM_PROJECTION_ID_KEY] == "p-explicit"

    def test_from_metadata_with_and_without_fallbacks(self):
        meta = {WORK_ITEM_PROJECTION_ID_KEY: "p-1", WORK_ITEM_TURN_TYPE_KEY: "report"}
        payload = work_item_identity_payload_from_metadata(meta)
        assert payload == {WORK_ITEM_PROJECTION_ID_KEY: "p-1", WORK_ITEM_TURN_TYPE_KEY: "report"}
        fallback_payload = work_item_identity_payload_from_metadata(
            {}, projection_id_fallback="fb-p", turn_type_fallback="followup"
        )
        assert fallback_payload == {
            WORK_ITEM_PROJECTION_ID_KEY: "fb-p",
            WORK_ITEM_TURN_TYPE_KEY: "followup",
        }

    def test_for_task(self):
        task = FakeTask(id="task-1", metadata={WORK_ITEM_TURN_TYPE_KEY: "execute"})
        payload = work_item_identity_payload_for_task(task)
        assert payload[WORK_ITEM_PROJECTION_ID_KEY] == "task-1"
        assert payload[WORK_ITEM_TURN_TYPE_KEY] == "execute"


class TestResultDeliveryId:
    def test_explicit_id_wins(self):
        task = FakeTask(id="task-1")
        assert result_delivery_id_for_task(task, result_delivery_id="rd-1") == "rd-1"

    def test_requires_task_id(self):
        task = FakeTask(id="")
        assert result_delivery_id_for_task(task) == ""

    def test_turn_and_execution_identity(self):
        task = FakeTask(id="task-1")
        turn_delivery = result_delivery_id_for_task(task, canonical_turn_id="turn-9")
        assert turn_delivery == "result:task:task-1:turn:turn-9:attempt:0"
        execution_delivery = result_delivery_id_for_task(task, execution_id="exec-3")
        assert execution_delivery == "result:task:task-1:execution:exec-3:attempt:0"

    def test_retry_attempt_suffix_and_clamping(self):
        task = FakeTask(id="task-1")
        task.retry_count = 2
        retry_delivery = result_delivery_id_for_task(task, canonical_turn_id="turn-9")
        assert retry_delivery == "result:task:task-1:turn:turn-9:attempt:2"
        task.retry_count = "nope"
        invalid_delivery = result_delivery_id_for_task(task, canonical_turn_id="turn-9")
        assert invalid_delivery == "result:task:task-1:turn:turn-9:attempt:0"
        task.retry_count = -3
        negative_delivery = result_delivery_id_for_task(task, canonical_turn_id="turn-9")
        assert negative_delivery == "result:task:task-1:turn:turn-9:attempt:0"


class TestCanonicalResultTurnId:
    def test_explicit_and_metadata_value(self):
        task = FakeTask()
        assert canonical_result_turn_id_for_task(task, canonical_turn_id="ct-1") == "ct-1"
        meta_task = FakeTask(metadata={"canonical_turn_id": "meta-turn"})
        assert canonical_result_turn_id_for_task(meta_task) == "meta-turn"

    def test_runtime_v2_current_turn_id(self):
        task = FakeTask(metadata={"runtime_v2": {"current_turn_id": "rt-turn"}})
        assert canonical_result_turn_id_for_task(task) == "rt-turn"
        scoped_task = FakeTask(
            metadata={
                "runtime_v2_current_turn_id": "top-level-only",
                "runtime_v2": {"runtime_v2_current_turn_id": "rt-scoped"},
            }
        )
        assert canonical_result_turn_id_for_task(scoped_task) == "rt-scoped"

    def test_runtime_v2_current_turn_id_ignored_at_top_level(self):
        task = FakeTask(metadata={"runtime_v2_current_turn_id": "top-level-only"})
        assert canonical_result_turn_id_for_task(task) == ""

    def test_empty_when_absent(self):
        assert canonical_result_turn_id_for_task(FakeTask()) == ""


class TestCanonicalResultTurnIdPriority:
    def test_canonical_beats_legacy(self):
        runtime_canonical_task = FakeTask(
            metadata={
                "conversation_turn_id": "legacy-turn",
                "runtime_v2": {"canonical_turn_id": "rt-can"},
            }
        )
        assert canonical_result_turn_id_for_task(runtime_canonical_task) == "rt-can"
        runtime_priority_task = FakeTask(
            metadata={"runtime_v2": {"canonical_turn_id": "rt-can", "current_turn_id": "rt-legacy"}}
        )
        assert canonical_result_turn_id_for_task(runtime_priority_task) == "rt-can"
        metadata_priority_task = FakeTask(
            metadata={"canonical_turn_id": "meta-can", "conversation_turn_id": "meta-legacy"}
        )
        assert canonical_result_turn_id_for_task(metadata_priority_task) == "meta-can"

    def test_blank_canonical_falls_through_to_legacy(self):
        task = FakeTask(metadata={"canonical_turn_id": "   ", "conversation_turn_id": "legacy"})
        assert canonical_result_turn_id_for_task(task) == "legacy"


class TestResultDeliveryIdentityPayload:
    def test_structure_and_empty_task(self):
        task = FakeTask(id="task-1", metadata={"canonical_turn_id": "turn-9"})
        payload = result_delivery_identity_payload_for_task(task, canonical_turn_id="turn-9")
        assert payload[RESULT_DELIVERY_ID_KEY] == "result:task:task-1:turn:turn-9:attempt:0"
        assert payload[SOURCE_TASK_ID_KEY] == "task-1"
        assert payload["canonical_turn_id"] == "turn-9"
        empty_payload = result_delivery_identity_payload_for_task(FakeTask(id=""))
        assert empty_payload == {}


class TestMigrateMetadata:
    def test_adds_keys_and_preserves_existing(self):
        result, changed = migrate_work_item_projection_metadata(
            {}, projection_id_fallback="p-1", turn_type_fallback="plan"
        )
        assert changed is True
        assert result[WORK_ITEM_PROJECTION_ID_KEY] == "p-1"
        assert result[WORK_ITEM_TURN_TYPE_KEY] == "plan"
        meta = {WORK_ITEM_PROJECTION_ID_KEY: "p-ex", WORK_ITEM_TURN_TYPE_KEY: "review"}
        preserved, changed_existing = migrate_work_item_projection_metadata(meta)
        assert changed_existing is False
        assert preserved[WORK_ITEM_PROJECTION_ID_KEY] == "p-ex"


class TestGateReworkProjection:
    def test_rework_projection_id_from_metadata_and_attribute(self):
        gate = FakeGate(metadata={GATE_REWORK_PROJECTION_ID_KEY: "rw-1"})
        assert rework_projection_id_for_gate(gate) == "rw-1"
        attr_gate = FakeGate(rework_projection_id="rw-attr")
        assert rework_projection_id_for_gate(attr_gate) == "rw-attr"

    def test_mark_gate_rework_projection_and_empty(self):
        gate = FakeGate()
        mark_gate_rework_projection(gate, "rw-2")
        assert gate.metadata[GATE_REWORK_PROJECTION_ID_KEY] == "rw-2"
        assert gate.rework_projection_id == "rw-2"
        empty_gate = FakeGate()
        mark_gate_rework_projection(empty_gate, "")
        assert GATE_REWORK_PROJECTION_ID_KEY not in empty_gate.metadata


class TestGateDecisionTargets:
    def test_single_and_multi_targets(self):
        decision = FakeDecision(target_projection_id="tp-1")
        assert target_projection_id_for_decision(decision) == "tp-1"
        multi_decision = FakeDecision(target_projection_ids=["a", "b", "a"])
        assert target_projection_ids_for_decision(multi_decision) == ["a", "b"]
        single_decision = FakeDecision(target_projection_id="tp-1")
        assert target_projection_ids_for_decision(single_decision) == ["tp-1"]

    def test_empty_decision(self):
        assert target_projection_ids_for_decision(FakeDecision()) == []

    def test_gate_rework_payload(self):
        payload = gate_rework_payload(
            rework_projection_id="rw-1", target_projection_id="tp-1", review_projection_id="rv-1"
        )
        assert payload == {
            GATE_REWORK_PROJECTION_ID_KEY: "rw-1",
            GATE_TARGET_PROJECTION_ID_KEY: "tp-1",
            "review_projection_id": "rv-1",
        }

    def test_gate_rework_payload_empty(self):
        assert gate_rework_payload() == {}


class TestGateProjectionBlankFallbacks:
    def test_rework_blank_metadata_and_attribute_use_fallback(self):
        gate = FakeGate(metadata={GATE_REWORK_PROJECTION_ID_KEY: "   "})
        assert rework_projection_id_for_gate(gate, fallback="fb-1") == "fb-1"
        attr_gate = FakeGate(rework_projection_id="   ")
        assert rework_projection_id_for_gate(attr_gate, fallback="fb-1") == "fb-1"

    def test_rework_valid_metadata_beats_blank_and_blank_everything(self):
        gate = FakeGate(
            metadata={GATE_REWORK_PROJECTION_ID_KEY: "rw-1"}, rework_projection_id="   "
        )
        assert rework_projection_id_for_gate(gate, fallback="fb-1") == "rw-1"
        blank_gate = FakeGate(rework_projection_id="   ")
        assert rework_projection_id_for_gate(blank_gate, fallback="   ") == ""

    def test_target_blank_uses_fallback_and_non_blank_kept(self):
        blank_decision = FakeDecision(target_projection_id="   ")
        assert target_projection_id_for_decision(blank_decision, fallback="fb-1") == "fb-1"
        kept_decision = FakeDecision(target_projection_id="tp-1")
        assert target_projection_id_for_decision(kept_decision, fallback="fb-1") == "tp-1"

    def test_target_blank_without_fallback(self):
        decision = FakeDecision(target_projection_id="   ")
        assert target_projection_id_for_decision(decision) == ""


class TestWorkItemTurnTypeEnumReuse:
    def test_canonical_set_matches_enum(self):
        expected = frozenset(t.value for t in WorkItemTurnType)
        assert len(expected) == 12
        assert "self_evolution" in expected


class TestCleanValueFalsyPassthrough:
    def test_falsy_values_pass_through_normalize(self):
        assert normalize_work_item_turn_type(0) == "0"
        assert normalize_work_item_turn_type(False) == "false"
        assert normalize_work_item_turn_type(None) == ""

    def test_zero_projection_passes_through(self):
        meta = {WORK_ITEM_PROJECTION_ID_KEY: 0}
        assert work_item_projection_id_from_metadata(meta, fallback="fb") == "0"


class TestNonMappingMetadataDefense:
    def test_task_metadata_string_falls_back_to_id(self):
        task = FakeTask(id="task-9")
        task.metadata = "not-a-mapping"
        assert projection_id_for_task(task) == "task-9"

    def test_task_metadata_sequences_not_converted(self):
        task = FakeTask(id="task-9")
        task.metadata = ["not", "pairs"]
        assert projection_id_for_task(task) == "task-9"
        pairs_task = FakeTask(id="task-9")
        pairs_task.metadata = [(WORK_ITEM_PROJECTION_ID_KEY, "proj-t")]
        assert projection_id_for_task(pairs_task) == "task-9"

    def test_work_item_metadata_string_falls_back_to_id(self):
        item = FakeWorkItem(work_item_id="wi-7")
        item.metadata = "not-a-mapping"
        assert projection_id_for_work_item(item) == "wi-7"

    def test_runtime_v2_non_mapping_ignored(self):
        task = FakeTask(metadata={"runtime_v2": "nope"})
        assert canonical_result_turn_id_for_task(task) == ""
        pairs_task = FakeTask(metadata={"runtime_v2": [("current_turn_id", "rt-turn")]})
        assert canonical_result_turn_id_for_task(pairs_task) == ""

    def test_gate_metadata_string_falls_back(self):
        gate = FakeGate(rework_projection_id="rw-attr")
        gate.metadata = "not-a-mapping"
        assert rework_projection_id_for_gate(gate) == "rw-attr"
