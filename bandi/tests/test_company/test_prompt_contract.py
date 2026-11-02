from __future__ import annotations

from datetime import UTC, datetime

from bandi.company.phase import Phase
from bandi.company.prompt_contract import (
    PROMPT_CONTRACT_VERSION,
    build_contract,
    has_prompt_contract,
    is_report_prompt_turn,
    make_prompt_contract,
    make_prompt_contract_blocker,
    normalize_dependency_specs,
    normalize_prompt_contract,
    normalize_prompt_text_list,
    prompt_contract_from_delegate_item,
    prompt_contract_from_work_item,
    render_assignment_context_from_contract,
    render_contract,
    render_target_prompt_contract,
    validate_contract,
)
from bandi.company.work_item import WorkItem, WorkItemTurnType


class _FakeWorkItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _make_work_item(turn_type: WorkItemTurnType) -> WorkItem:
    return WorkItem(
        id="wi-real",
        title="real",
        description="",
        phase=Phase.READY,
        turn_type=turn_type,
        assignee_role=None,
        parent_id=None,
        children_ids=[],
        links={},
        metadata={},
        runtime_metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class TestPromptContract:
    def test_build_minimal(self):
        contract = build_contract(
            work_item_id="wi1", title="test", turn_type="execute", phase="running"
        )
        assert contract["work_item_id"] == "wi1"
        assert contract["version"] == 2

    def test_build_full(self):
        contract = build_contract(
            work_item_id="wi1",
            title="Test Task",
            turn_type="execute",
            phase="queued",
            assignee_role="worker",
            description="Do the thing",
            parent_context="parent-ctx",
            dependencies=["dep1"],
            output_format="JSON",
            extra={"priority": "high"},
        )
        assert contract["assignee_role"] == "worker"
        assert contract["extra"]["priority"] == "high"

    def test_render_contract(self):
        contract = build_contract(
            work_item_id="wi1", title="Test", turn_type="execute", phase="running"
        )
        rendered = render_contract(contract)
        assert "Test" in rendered
        assert "execute" in rendered

    def test_render_contract_includes_extra(self):
        contract = build_contract(
            work_item_id="wi1",
            title="Test",
            turn_type="execute",
            phase="running",
            extra={"priority": "high", "risk": "medium"},
        )
        rendered = render_contract(contract)
        assert "priority: high" in rendered
        assert "risk: medium" in rendered

    def test_validate_valid(self):
        contract = build_contract(
            work_item_id="wi1", title="test", turn_type="execute", phase="queued"
        )
        assert validate_contract(contract) == []

    def test_validate_missing_fields(self):
        errors = validate_contract({"version": 1})
        assert len(errors) >= 2
        assert any("work_item_id" in e for e in errors)
        assert any("title" in e for e in errors)

    def test_validate_missing_phase(self):
        contract = build_contract(work_item_id="wi1", title="test", turn_type="execute", phase="")
        errors = validate_contract(contract)
        assert any("phase" in e for e in errors)

    def test_build_contract_deepcopies_extra(self):
        extra = {"nested": {"deep": [1, 2]}}
        contract = build_contract(
            work_item_id="wi1", title="test", turn_type="execute", phase="queued", extra=extra
        )
        contract["extra"]["nested"]["deep"].append(3)
        assert extra["nested"]["deep"] == [1, 2]


class TestPromptContractV2Version:
    def test_version_constant(self):
        assert PROMPT_CONTRACT_VERSION == 2


class TestNormalizePromptTextList:
    def test_none_and_empty(self):
        assert normalize_prompt_text_list(None) == []
        assert normalize_prompt_text_list("") == []

    def test_list_of_str(self):
        assert normalize_prompt_text_list(["a", "  b  "]) == ["a", "b"]

    def test_list_of_dicts(self):
        items = normalize_prompt_text_list(
            [{"value": "v"}, {"input": "i"}, {"raw": "r"}, {"work_item_id": "w"}]
        )
        assert items == ["v", "i", "r", "w"]

    def test_dict_missing_all_keys_filtered(self):
        assert normalize_prompt_text_list([{"other": "x"}]) == []

    def test_single_string(self):
        assert normalize_prompt_text_list("single") == ["single"]

    def test_set_input(self):
        assert set(normalize_prompt_text_list({"a", "b"})) == {"a", "b"}


class TestNormalizeDependencySpecs:
    def test_none(self):
        assert normalize_dependency_specs(None) == []

    def test_list_kept(self):
        specs = normalize_dependency_specs(["a", {"id": "b"}])
        assert specs == ["a", {"id": "b"}]

    def test_dict_wrapped(self):
        assert normalize_dependency_specs({"id": "x"}) == [{"id": "x"}]

    def test_string(self):
        assert normalize_dependency_specs("dep1") == ["dep1"]

    def test_empty_strings_filtered(self):
        assert normalize_dependency_specs(["", "a", "  "]) == ["a"]

    def test_none_item_filtered(self):
        assert normalize_dependency_specs(["a", None, {"id": "b"}]) == ["a", {"id": "b"}]

    def test_none_only(self):
        assert normalize_dependency_specs([None]) == []


class TestNormalizePromptTextListNoneItems:
    def test_none_item_filtered(self):
        assert normalize_prompt_text_list(["a", None, "b"]) == ["a", "b"]

    def test_none_only(self):
        assert normalize_prompt_text_list([None]) == []

    def test_zero_preserved(self):
        assert normalize_prompt_text_list(["a", 0]) == ["a", "0"]


class TestManagerOutcomeDispatchBoolParsing:
    def test_string_false(self):
        contract = make_prompt_contract(task_brief="x", manager_outcome_dispatch="false")
        assert contract["assignment_context"]["manager_outcome_dispatch"] is False

    def test_string_zero(self):
        contract = make_prompt_contract(task_brief="x", manager_outcome_dispatch="0")
        assert contract["assignment_context"]["manager_outcome_dispatch"] is False

    def test_string_true(self):
        contract = make_prompt_contract(task_brief="x", manager_outcome_dispatch="true")
        assert contract["assignment_context"]["manager_outcome_dispatch"] is True

    def test_numeric_one(self):
        contract = make_prompt_contract(task_brief="x", manager_outcome_dispatch=1)
        assert contract["assignment_context"]["manager_outcome_dispatch"] is True

    def test_string_false_normalized_contract(self):
        contract = normalize_prompt_contract(
            {"task_brief": "x", "assignment_context": {"manager_outcome_dispatch": "false"}}
        )
        assert contract["assignment_context"]["manager_outcome_dispatch"] is False

    def test_legacy_work_item_string_false(self):
        wi = _FakeWorkItem(
            work_item_id="wi1",
            metadata={
                "prompt_assignment": {
                    "task_brief": "brief",
                    "manager_outcome_dispatch": "false",
                }
            },
            title="t",
            summary="s",
        )
        contract = prompt_contract_from_work_item(wi)
        assert contract["assignment_context"]["manager_outcome_dispatch"] is False


class TestNormalizePromptContract:
    def test_non_dict_returns_empty_structure(self):
        normalized = normalize_prompt_contract(None)
        assert normalized["version"] == 2
        assert normalized["task_brief"] == ""
        assert normalized["assignment_context"]["deliverables"] == []
        assert normalized["assignment_context"]["owned_outcome_kind"] == "execute"

    def test_invalid_version_defaults(self):
        normalized = normalize_prompt_contract({"version": "nope", "task_brief": "b"})
        assert normalized["version"] == 2

    def test_task_brief_stripped(self):
        normalized = normalize_prompt_contract({"task_brief": "  brief  "})
        assert normalized["task_brief"] == "brief"

    def test_assignment_fields_normalized(self):
        normalized = normalize_prompt_contract(
            {
                "assignment_context": {
                    "upstream_intent_summary": "  intent ",
                    "manager_outcome_dispatch": 1,
                    "owned_outcome_kind": "",
                    "deliverables": ["d1", "d2"],
                }
            }
        )
        assignment = normalized["assignment_context"]
        assert assignment["upstream_intent_summary"] == "intent"
        assert assignment["manager_outcome_dispatch"] is True
        assert assignment["owned_outcome_kind"] == "execute"
        assert assignment["deliverables"] == ["d1", "d2"]

    def test_target_contract_recursive(self):
        normalized = normalize_prompt_contract({"target_contract": {"task_brief": "  t "}})
        assert normalized["target_contract"]["task_brief"] == "t"

    def test_source_kept(self):
        normalized = normalize_prompt_contract({"source": {"kind": "x"}})
        assert normalized["source"] == {"kind": "x"}

    def test_turn_profiles_kept(self):
        normalized = normalize_prompt_contract({"turn_profiles": {"p": {"enabled": True}}})
        assert normalized["turn_profiles"] == {"p": {"enabled": True}}


class TestHasPromptContract:
    def test_task_brief_only(self):
        assert has_prompt_contract({"task_brief": "do it"})

    def test_assignment_content_only(self):
        assert has_prompt_contract({"assignment_context": {"deliverables": ["d"]}})

    def test_empty(self):
        assert not has_prompt_contract({})
        assert not has_prompt_contract(None)


class TestMakePromptContract:
    def test_full(self):
        contract = make_prompt_contract(
            task_brief="Build feature",
            upstream_intent_summary="parent goal",
            manager_outcome_dispatch=True,
            owned_outcome_kind="deliverable",
            scope_key="scope-a",
            deliverables=["impl", "docs"],
            acceptance_criteria=["works"],
            dependency_specs=["wi-1"],
            coordination_notes="sync with team",
            delegation_rationale="focus",
            non_overlap_guard="stay in module",
            turn_profiles={"execute": {"max_turns": 5}},
            target_contract={"task_brief": "child"},
            source={"kind": "test"},
        )
        assert contract["version"] == 2
        assert contract["task_brief"] == "Build feature"
        assert contract["assignment_context"]["upstream_intent_summary"] == "parent goal"
        assert contract["assignment_context"]["owned_outcome_kind"] == "deliverable"
        assert contract["turn_profiles"]["execute"]["max_turns"] == 5
        assert contract["target_contract"]["task_brief"] == "child"
        assert contract["source"] == {"kind": "test"}

    def test_defaults(self):
        contract = make_prompt_contract(task_brief="t")
        assignment = contract["assignment_context"]
        assert assignment["owned_outcome_kind"] == "execute"
        assert assignment["manager_outcome_dispatch"] is False
        assert assignment["deliverables"] == []

    def test_blocker(self):
        blocker = make_prompt_contract_blocker("missing brief")
        assert "SYSTEM BLOCKER" in blocker["task_brief"]
        assert blocker["source"]["kind"] == "prompt_contract_blocker"
        assert "missing brief" in blocker["assignment_context"]["acceptance_criteria"][0]


class TestPromptContractFromDelegateItem:
    def test_deliverables_used(self):
        contract = prompt_contract_from_delegate_item(
            {"deliverables": ["d1"], "acceptance_criteria": ["ac1"], "coordination_notes": "n"},
            task_brief="delegate task",
        )
        assert contract["assignment_context"]["deliverables"] == ["d1"]
        assert contract["assignment_context"]["acceptance_criteria"] == ["ac1"]
        assert contract["source"]["kind"] == "delegate_work"

    def test_outputs_and_done_when_fallback(self):
        contract = prompt_contract_from_delegate_item(
            {"outputs": ["o1"], "done_when": ["dw"]},
            task_brief="delegate task",
        )
        assert contract["assignment_context"]["deliverables"] == ["o1"]
        assert contract["assignment_context"]["acceptance_criteria"] == ["dw"]


class TestPromptContractFromWorkItem:
    def test_existing_contract_reused(self):
        item = _FakeWorkItem(
            metadata={"prompt_contract": {"task_brief": "existing brief"}},
            summary="legacy",
            title="title",
            kind="execute",
        )
        contract = prompt_contract_from_work_item(item)
        assert contract["task_brief"] == "existing brief"

    def test_legacy_prompt_assignment(self):
        item = _FakeWorkItem(
            metadata={
                "prompt_assignment": {"task_brief": "from assignment", "deliverables": ["d"]}
            },
            summary="",
            title="",
        )
        contract = prompt_contract_from_work_item(item)
        assert contract["task_brief"] == "from assignment"
        assert contract["assignment_context"]["deliverables"] == ["d"]

    def test_duck_typing_summary_fallback(self):
        item = _FakeWorkItem(metadata={}, summary="summary brief", title="ignored")
        contract = prompt_contract_from_work_item(item)
        assert contract["task_brief"] == "summary brief"

    def test_no_brief_returns_blocker(self):
        item = _FakeWorkItem(metadata={}, summary="", title="")
        contract = prompt_contract_from_work_item(item)
        assert "BLOCKER" in contract["task_brief"]

    def test_work_item_id_in_blocker_reason(self):
        item = _FakeWorkItem(metadata={}, summary="", title="", work_item_id="wi-9")
        contract = prompt_contract_from_work_item(item)
        assert "wi-9" in contract["assignment_context"]["acceptance_criteria"][0]


class TestRenderAssignmentContext:
    def test_empty_contract(self):
        assert render_assignment_context_from_contract({}) == ""

    def test_dispatch_fields(self):
        rendered = render_assignment_context_from_contract(
            {
                "assignment_context": {
                    "manager_outcome_dispatch": True,
                    "owned_outcome_kind": "plan",
                }
            },
            include_dispatch_fields=True,
        )
        assert "Manager outcome turn" in rendered
        assert "plan" in rendered

    def test_dispatch_hidden_without_flag(self):
        rendered = render_assignment_context_from_contract(
            {"assignment_context": {"manager_outcome_dispatch": True}},
            include_dispatch_fields=False,
        )
        assert "Manager outcome turn" not in rendered

    def test_sections(self):
        rendered = render_assignment_context_from_contract(
            {
                "assignment_context": {
                    "upstream_intent_summary": "intent",
                    "scope_key": "scope",
                    "deliverables": ["d1"],
                    "coordination_notes": "note",
                }
            }
        )
        assert "### Upstream Intent Summary" in rendered
        assert "### Scope Key" in rendered
        assert "### Deliverables" in rendered
        assert "### Coordination Notes" in rendered


class TestRenderTargetContract:
    def test_empty(self):
        assert render_target_prompt_contract({}) == ""

    def test_brief_and_assignment(self):
        rendered = render_target_prompt_contract(
            {
                "task_brief": "target brief",
                "assignment_context": {"deliverables": ["d"]},
            }
        )
        assert "### Target Work Item Contract" in rendered
        assert "#### Task Brief" in rendered
        assert "target brief" in rendered
        assert "#### Assignment Context" in rendered


class TestIsReportPromptTurn:
    def test_report_execution_work_item(self):
        assert is_report_prompt_turn({"report_execution_work_item": "wi-1"})

    def test_turn_mode(self):
        assert is_report_prompt_turn({"current_turn_mode": "report_required"})

    def test_work_item_turn_type(self):
        assert is_report_prompt_turn({"work_item_turn_type": "report"})

    def test_work_kind(self):
        assert is_report_prompt_turn({"work_kind": "report"})

    def test_false(self):
        assert not is_report_prompt_turn({})
        assert not is_report_prompt_turn({"work_kind": "execute"})


class TestPromptContractFromRealWorkItem:
    def test_owned_outcome_kind_falls_back_to_turn_type(self):
        item = _make_work_item(WorkItemTurnType.PLAN)
        contract = prompt_contract_from_work_item(item, task_description="plan it")
        assert contract["assignment_context"]["owned_outcome_kind"] == "plan"

    def test_metadata_owned_outcome_kind_precedence(self):
        item = _make_work_item(WorkItemTurnType.PLAN)
        item.metadata["owned_outcome_kind"] = "deliverable"
        contract = prompt_contract_from_work_item(item, task_description="plan it")
        assert contract["assignment_context"]["owned_outcome_kind"] == "deliverable"

    def test_kind_attribute_precedence_over_turn_type(self):
        item = _make_work_item(WorkItemTurnType.EXECUTE)
        item.kind = "report"
        contract = prompt_contract_from_work_item(item, task_description="do it")
        assert contract["assignment_context"]["owned_outcome_kind"] == "report"


class TestPromptContractRobustness:
    def test_owned_outcome_kind_counts_as_content_when_non_default(self):
        contract = make_prompt_contract(task_brief="", owned_outcome_kind="report")
        assert has_prompt_contract(contract) is True

    def test_owned_outcome_kind_default_execute_not_content(self):
        contract = make_prompt_contract(task_brief="", owned_outcome_kind="execute")
        assert has_prompt_contract(contract) is False

    def test_none_value_bypasses_default_fixed(self):
        item = {"deliverables": None, "outputs": ["output-a"], "done_when": None}
        contract = prompt_contract_from_delegate_item(item, task_brief="brief")
        assert contract["assignment_context"]["deliverables"] == ["output-a"]

    def test_false_string_does_not_trigger_report(self):
        assert is_report_prompt_turn({"report_execution_work_item": "false"}) is False
        assert is_report_prompt_turn({"report_execution_work_item": "0"}) is False
        assert is_report_prompt_turn({"report_execution_work_item": True}) is True

    def test_non_dict_assignment_context_does_not_crash(self):
        contract = normalize_prompt_contract({"assignment_context": ["not", "a", "mapping"]})
        assert contract["assignment_context"]["owned_outcome_kind"] == "execute"

    def test_non_dict_source_does_not_crash(self):
        contract = normalize_prompt_contract({"source": ["not", "a", "mapping"]})
        assert "source" not in contract

    def test_non_dict_payload_does_not_crash(self):
        for bad in (["not", "a", "mapping"], None, "text"):
            normalized = normalize_prompt_contract(bad)
            assert normalized["assignment_context"]["owned_outcome_kind"] == "execute"
            assert not has_prompt_contract(bad)
