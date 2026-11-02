from __future__ import annotations

from types import SimpleNamespace

from bandi.company.metadata_ownership import (
    EXECUTION_COPY_KEYS,
    METADATA_FIELD_SPECS,
    RUNTIME_TASK_OWNED_KEYS,
    WORK_ITEM_OWNED_KEYS,
    MetadataOwner,
    build_work_item_owner_execution_copy,
    check_ownership,
    copy_work_item_execution_metadata,
    filter_runtime_task_owned_metadata,
    filter_work_item_owned_metadata,
    get_owner,
    get_spec,
    is_execution_copy_key,
    is_runtime_task_owned_key,
    is_work_item_owned_key,
    list_keys_by_owner,
    metadata_owner_for_key,
    metadata_spec_for_key,
    strip_disallowed_work_item_metadata_from_runtime_task,
    supports_legacy_task_fallback,
    update_runtime_task_owned_metadata,
    validate_metadata,
    validate_metadata_ownership,
)

_MIGRATION_POLICIES = {
    "work_item_wins",
    "backfill_if_missing",
    "task_only",
    "copy_from_runtime_context",
}


class TestMetadataOwnership:
    def test_get_owner_resolves_owned_and_default_owner(self):
        assert get_owner("phase") == MetadataOwner.WORK_ITEM
        assert get_owner("title") == MetadataOwner.WORK_ITEM
        assert get_owner("session_id") == MetadataOwner.RUNTIME_TASK
        assert get_owner("nonexistent") == MetadataOwner.RUNTIME_TASK

    def test_get_spec_returns_spec_or_none(self):
        spec = get_spec("phase")
        assert spec is not None
        assert spec.key == "phase"
        assert get_spec("nonexistent") is None

    def test_check_ownership_returns_none_or_violation(self):
        assert check_ownership("title", "work_item") is None
        issue = check_ownership("title", "task_execution_copy")
        assert issue is not None
        assert issue.code == "ownership_violation"

    def test_list_keys_by_owner_contains_work_item_keys(self):
        work_item_keys = list_keys_by_owner(MetadataOwner.WORK_ITEM)
        assert "phase" in work_item_keys
        assert "title" in work_item_keys

    def test_validate_metadata_reports_clean_and_violating(self):
        issues = validate_metadata({"custom_key": "value"}, "work_item")
        assert len(issues) == 0
        issues = validate_metadata({"phase": "running", "title": "test"}, "task_execution_copy")
        assert len(issues) == 2

    def test_supports_legacy_task_fallback_false_for_unregistered_keys(self):
        assert supports_legacy_task_fallback("nonexistent") is False
        assert supports_legacy_task_fallback("session_id") is False
        assert supports_legacy_task_fallback("phase") is False


class TestFieldMatrix:
    def test_field_spec_volume_reaches_reference_thresholds(self):
        assert len(METADATA_FIELD_SPECS) >= 163
        work_item_count = sum(
            1 for s in METADATA_FIELD_SPECS.values() if s.owner == MetadataOwner.WORK_ITEM
        )
        assert work_item_count >= 96
        runtime_task_count = sum(
            1 for s in METADATA_FIELD_SPECS.values() if s.owner == MetadataOwner.RUNTIME_TASK
        )
        assert runtime_task_count >= 19
        execution_copy_count = sum(
            1 for s in METADATA_FIELD_SPECS.values() if s.owner == MetadataOwner.EXECUTION_COPY
        )
        assert execution_copy_count >= 48

    def test_legacy_and_reference_fields_registered(self):
        for key in (
            "phase",
            "turn_type",
            "assignee_role",
            "parent_id",
            "children_ids",
            "title",
            "description",
            "links",
            "session_id",
            "execution_log",
            "tool_history",
            "error_info",
            "last_transition_reason",
            "output_contract_result",
        ):
            assert key in METADATA_FIELD_SPECS, f"missing legacy field {key}"
        for key in (
            "work_kind",
            "current_turn_mode",
            "prompt_contract",
            "manager_mutation_reason",
            "progress_log",
            "completion_report",
            "verification_status",
            "review_attempt",
            "rework_feedback",
            "delivery_package",
            "self_evolution_work_item",
            "runtime_v2",
            "working_memory",
            "peer_wait",
            "organization_id",
            "delegation_run_id",
            "manager_role_id",
            "selected_execution_agent",
            "target_output_dir",
            "work_item_execution_strategy",
        ):
            assert key in METADATA_FIELD_SPECS, f"missing reference field {key}"

    def test_migration_policies_valid_for_all_specs_and_samples(self):
        for spec in METADATA_FIELD_SPECS.values():
            assert spec.migration_policy in _MIGRATION_POLICIES or spec.migration_policy == "", (
                f"{spec.key}: {spec.migration_policy}"
            )
        for key in ("work_kind", "runtime_v2", "delegation_run_id"):
            assert METADATA_FIELD_SPECS[key].migration_policy in _MIGRATION_POLICIES

    def test_parent_session_id_aligned_to_execution_copy(self):
        assert METADATA_FIELD_SPECS["parent_session_id"].owner == MetadataOwner.EXECUTION_COPY

    def test_allows_task_legacy_read_property(self):
        spec = METADATA_FIELD_SPECS["progress_log"]
        assert spec.allows_task_legacy_read is True
        assert METADATA_FIELD_SPECS["title"].allows_task_legacy_read is False

    def test_allows_task_execution_copy_property(self):
        assert METADATA_FIELD_SPECS["work_kind"].allows_task_execution_copy is True
        assert METADATA_FIELD_SPECS["title"].allows_task_execution_copy is False


class TestDerivedKeySets:
    def test_derived_key_sets_match_owner(self):
        assert WORK_ITEM_OWNED_KEYS == frozenset(
            k for k, s in METADATA_FIELD_SPECS.items() if s.owner == MetadataOwner.WORK_ITEM
        )
        assert RUNTIME_TASK_OWNED_KEYS == frozenset(
            k for k, s in METADATA_FIELD_SPECS.items() if s.owner == MetadataOwner.RUNTIME_TASK
        )
        expected_execution_copy = frozenset(
            k
            for k, s in METADATA_FIELD_SPECS.items()
            if s.owner == MetadataOwner.EXECUTION_COPY or s.allows_task_execution_copy
        )
        assert EXECUTION_COPY_KEYS == expected_execution_copy

    def test_owned_key_sets_relations(self):
        assert WORK_ITEM_OWNED_KEYS <= frozenset(METADATA_FIELD_SPECS)
        assert WORK_ITEM_OWNED_KEYS.isdisjoint(RUNTIME_TASK_OWNED_KEYS)


class TestQueryApi:
    def test_metadata_owner_for_key_lookup(self):
        assert metadata_owner_for_key("work_kind") == MetadataOwner.WORK_ITEM
        assert metadata_owner_for_key("runtime_v2") == MetadataOwner.RUNTIME_TASK
        assert metadata_owner_for_key("delegation_run_id") == MetadataOwner.EXECUTION_COPY
        assert metadata_owner_for_key("unknown") is None
        assert metadata_owner_for_key("") is None
        assert metadata_owner_for_key(None) is None

    def test_metadata_spec_for_key_lookup(self):
        spec = metadata_spec_for_key("work_kind")
        assert spec is not None and spec.key == "work_kind"
        assert metadata_spec_for_key("nope") is None

    def test_ownership_predicates(self):
        assert is_work_item_owned_key("work_kind") is True
        assert is_work_item_owned_key("runtime_v2") is False
        assert is_work_item_owned_key("") is False
        assert is_runtime_task_owned_key("runtime_v2") is True
        assert is_runtime_task_owned_key("work_kind") is False
        assert is_execution_copy_key("delegation_run_id") is True
        assert is_execution_copy_key("work_kind") is True
        assert is_execution_copy_key("runtime_v2") is False

    def test_supports_legacy_task_fallback_true_and_false(self):
        assert supports_legacy_task_fallback("work_kind") is True
        assert supports_legacy_task_fallback("prompt_contract") is False
        assert supports_legacy_task_fallback("") is False


class TestCopyExecutionMetadata:
    def test_copy_none_work_item(self):
        assert copy_work_item_execution_metadata(None) == {}

    def test_copy_copies_allowed_execution_copy_keys(self):
        wi = SimpleNamespace(metadata={"work_kind": "deliver", "title": "x", "runtime_v2": "stub"})
        copied = copy_work_item_execution_metadata(wi)
        assert copied["work_kind"] == "deliver"
        assert "title" not in copied
        assert "runtime_v2" not in copied

    def test_copy_filters_by_keys_arg(self):
        wi = SimpleNamespace(metadata={"work_kind": "deliver", "current_turn_mode": "execute"})
        copied = copy_work_item_execution_metadata(wi, keys=["current_turn_mode"])
        assert copied == {"current_turn_mode": "execute"}

    def test_copy_skips_empty_values(self):
        wi = SimpleNamespace(metadata={"work_kind": "", "current_turn_mode": None})
        assert copy_work_item_execution_metadata(wi) == {}

    def test_copy_deepcopies_values(self):
        inner = {"a": 1}
        wi = SimpleNamespace(metadata={"work_kind": inner})
        copied = copy_work_item_execution_metadata(wi)
        assert copied["work_kind"] == inner
        copied["work_kind"]["a"] = 99
        assert inner["a"] == 1

    def test_default_keys_use_module_level_sorted_cache(self):
        from bandi.company.metadata_ownership import _EXECUTION_COPY_KEYS_SORTED

        assert _EXECUTION_COPY_KEYS_SORTED == tuple(sorted(EXECUTION_COPY_KEYS))


class TestBuildOwnerExecutionCopy:
    def test_none_work_item(self):
        assert build_work_item_owner_execution_copy(None) == {}

    def test_maps_identity_attributes(self):
        wi = SimpleNamespace(
            run_id="r1",
            cell_id="c1",
            team_instance_id="ti1",
            team_id="t1",
            seat_id="s1",
            role_runtime_session_id="rs1",
            role_id="role1",
            manager_role_id="mr1",
            manager_seat_id="ms1",
            batch_id="b1",
            kind="deliver",
            metadata={},
        )
        payload = build_work_item_owner_execution_copy(wi)
        assert payload["delegation_run_id"] == "r1"
        assert payload["delegation_cell_id"] == "c1"
        assert payload["delegation_team_id"] == "t1"
        assert payload["delegation_seat_id"] == "s1"
        assert payload["delegation_role_session_id"] == "rs1"
        assert payload["work_item_role_id"] == "role1"
        assert payload["manager_role_id"] == "mr1"
        assert payload["manager_seat_id"] == "ms1"
        assert payload["work_item_batch_id"] == "b1"
        assert payload["work_kind"] == "deliver"

    def test_uses_metadata_fallbacks(self):
        wi = SimpleNamespace(
            run_id="r1",
            cell_id=None,
            team_instance_id=None,
            team_id=None,
            seat_id=None,
            role_runtime_session_id=None,
            role_id=None,
            manager_role_id=None,
            manager_seat_id=None,
            batch_id=None,
            kind=None,
            metadata={"seat_id": "meta-seat", "work_kind": "review"},
        )
        payload = build_work_item_owner_execution_copy(wi)
        assert payload["delegation_seat_id"] == "meta-seat"
        assert payload["work_kind"] == "review"

    def test_only_includes_execution_copy_keys(self):
        wi = SimpleNamespace(run_id="r1", metadata={"non_managed": 1})
        payload = build_work_item_owner_execution_copy(wi)
        for key in payload:
            assert key in EXECUTION_COPY_KEYS

    def test_drops_empty_values(self):
        wi = SimpleNamespace(metadata={})
        assert build_work_item_owner_execution_copy(wi) == {}

    def test_work_kind_case_preserved_from_kind_and_metadata(self):
        wi = SimpleNamespace(run_id="r1", kind="DELIVER", metadata={})
        assert build_work_item_owner_execution_copy(wi)["work_kind"] == "DELIVER"
        wi = SimpleNamespace(run_id="r1", kind=None, metadata={"work_kind": "Review"})
        assert build_work_item_owner_execution_copy(wi)["work_kind"] == "Review"


class TestStripAndFilter:
    def test_strip_disallowed_work_item_keys(self):
        task = SimpleNamespace(metadata={"work_kind": "x", "title": "t", "prompt_contract": "p"})
        removed = strip_disallowed_work_item_metadata_from_runtime_task(task)
        assert removed == ["title"]
        assert task.metadata == {"work_kind": "x", "prompt_contract": "p"}
        task = SimpleNamespace(metadata={"runtime_v2": "r"})
        assert strip_disallowed_work_item_metadata_from_runtime_task(task) == []
        assert task.metadata == {"runtime_v2": "r"}
        task = SimpleNamespace(metadata={})
        assert strip_disallowed_work_item_metadata_from_runtime_task(task) == []

    def test_filter_work_item_owned(self):
        updates = {"work_kind": "x", "runtime_v2": "r", "unknown": "u", "": "empty"}
        filtered = filter_work_item_owned_metadata(updates)
        assert filtered == {"work_kind": "x"}

    def test_filter_runtime_task_owned(self):
        updates = {"runtime_v2": "r", "work_kind": "x", "unknown": "u"}
        filtered = filter_runtime_task_owned_metadata(updates)
        assert filtered == {"runtime_v2": "r"}

    def test_filter_work_item_drops_empty_and_none(self):
        assert filter_work_item_owned_metadata({"work_kind": "", "current_turn_mode": None}) == {}
        assert filter_work_item_owned_metadata(None) == {}

    def test_filter_preserve_empty_for_work_item_and_runtime(self):
        updates = {"work_kind": "", "current_turn_mode": None}
        assert filter_work_item_owned_metadata(updates, preserve_empty=True) == updates
        assert filter_work_item_owned_metadata(None, preserve_empty=True) == {}
        updates = {"runtime_v2": "", "work_kind": "x"}
        assert filter_runtime_task_owned_metadata(updates, preserve_empty=True) == {
            "runtime_v2": ""
        }


class TestPreserveEmptyUpdates:
    def test_update_runtime_preserves_empty(self):
        task = SimpleNamespace(metadata={"session_id": "s1"})
        updated = update_runtime_task_owned_metadata(task, {"runtime_v2": ""}, preserve_empty=True)
        assert updated == {"runtime_v2": ""}
        assert task.metadata["runtime_v2"] == ""


class TestValidateMetadataOwnership:
    def test_valid_pairs_produce_no_issues(self):
        assert validate_metadata_ownership(None, None) == []
        wi = SimpleNamespace(metadata={"work_kind": "deliver"})
        task = SimpleNamespace(id="task-1", metadata={"runtime_v2": "r"})
        assert validate_metadata_ownership(wi, task) == []
        wi = SimpleNamespace(metadata={})
        task = SimpleNamespace(id="task-1", metadata={"work_kind": ""})
        assert validate_metadata_ownership(wi, task) == []

    def test_owner_location_violations(self):
        wi = SimpleNamespace(metadata={})
        task = SimpleNamespace(id="task-1", metadata={"work_kind": "deliver"})
        issues = validate_metadata_ownership(wi, task)
        assert len(issues) == 1
        assert issues[0].code == "metadata_ownership_violation"
        assert issues[0].owner == MetadataOwner.WORK_ITEM.value
        wi = SimpleNamespace(metadata={"runtime_v2": "r"})
        task = SimpleNamespace(id="task-1", metadata={})
        issues = validate_metadata_ownership(wi, task)
        assert len(issues) == 1
        assert issues[0].code == "metadata_ownership_violation"
        assert issues[0].owner == MetadataOwner.RUNTIME_TASK.value

    def test_conflict_detection_and_execution_copy_suppression(self):
        wi = SimpleNamespace(metadata={"review_attempt_count": 2})
        task = SimpleNamespace(id="task-1", metadata={"review_attempt_count": 3})
        issues = validate_metadata_ownership(wi, task)
        assert len(issues) == 1
        assert issues[0].code == "metadata_ownership_conflict"
        assert issues[0].details["work_item_value"] == 2
        wi = SimpleNamespace(metadata={"work_kind": "deliver"})
        task = SimpleNamespace(id="task-1", metadata={"work_kind": "review"})
        assert validate_metadata_ownership(wi, task) == []

    def test_update_runtime_task_owned_metadata_no_change(self):
        task = SimpleNamespace(metadata={})
        assert update_runtime_task_owned_metadata(task, {"work_kind": "x"}) == {}
        assert task.metadata == {}


class TestOwnershipContractFields:
    CONTRACT_KEYS = (
        "ownership_contract",
        "work_item_gate",
        "execution_mode",
        "handoff_log",
    )

    def test_contract_keys_registered_and_in_owned_set(self):
        for key in self.CONTRACT_KEYS:
            assert key in METADATA_FIELD_SPECS, f"missing contract field {key}"
        for key in self.CONTRACT_KEYS:
            assert key in WORK_ITEM_OWNED_KEYS

    def test_contract_keys_owned_by_work_item_only(self):
        for key in self.CONTRACT_KEYS:
            assert metadata_owner_for_key(key) == MetadataOwner.WORK_ITEM
            assert is_work_item_owned_key(key)
            assert not is_runtime_task_owned_key(key)

    def test_contract_keys_supported_in_execution_copy(self):
        for key in self.CONTRACT_KEYS:
            spec = metadata_spec_for_key(key)
            assert spec is not None
            assert spec.allows_task_execution_copy

    def test_contract_keys_validate_as_work_item_location(self):
        for key in self.CONTRACT_KEYS:
            issue = check_ownership(key, "work_item")
            assert issue is None, f"{key} should be valid at work_item"
            issue_copy = check_ownership(key, "task_execution_copy")
            assert issue_copy is None, f"{key} should be valid at task_execution_copy"

    def test_contract_keys_filtered_by_owner(self):
        updates = {key: {"write_scope": "assigned_workspace"} for key in self.CONTRACT_KEYS}
        filtered = filter_work_item_owned_metadata(updates)
        assert set(filtered.keys()) == set(self.CONTRACT_KEYS)
        updates = {key: {"nested": 1} for key in self.CONTRACT_KEYS}
        filtered = filter_runtime_task_owned_metadata(updates)
        assert filtered == {}


class TestHasValueEmptyContainers:
    def test_empty_tuple_filtered(self):
        assert filter_work_item_owned_metadata({"work_kind": ()}) == {}

    def test_empty_set_filtered(self):
        assert filter_work_item_owned_metadata({"work_kind": set()}) == {}

    def test_empty_runtime_container_filtered(self):
        assert filter_runtime_task_owned_metadata({"runtime_v2": (), "session_id": "x"}) == {
            "session_id": "x"
        }

    def test_non_scalar_values_do_not_crash(self):
        class Uncomparable:
            def __eq__(self, other):
                return NotImplemented

        result = filter_work_item_owned_metadata({"work_kind": Uncomparable()})
        assert "work_kind" in result

    def test_preserve_empty_still_keeps_empty_containers(self):
        updates = {"work_kind": (), "current_turn_mode": set()}
        assert filter_work_item_owned_metadata(updates, preserve_empty=True) == updates
