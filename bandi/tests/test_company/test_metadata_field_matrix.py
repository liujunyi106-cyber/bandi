from __future__ import annotations

from bandi.company.metadata_field_matrix import (
    _WORK_ITEM_FIELDS,
    EXECUTION_COPY_KEYS,
    METADATA_FIELD_SPECS,
    RUNTIME_TASK_OWNED_KEYS,
    WORK_ITEM_OWNED_KEYS,
    MetadataFieldSpec,
    MetadataOwner,
)


class TestMetadataFieldSpec:
    def test_default_allowed_locations_work_item(self) -> None:
        spec = MetadataFieldSpec(key="x", owner=MetadataOwner.WORK_ITEM)
        assert spec.allowed_locations == ("work_item",)

    def test_default_allowed_locations_runtime_task(self) -> None:
        spec = MetadataFieldSpec(key="x", owner=MetadataOwner.RUNTIME_TASK)
        assert spec.allowed_locations == ("task",)

    def test_explicit_allowed_locations_preserved(self) -> None:
        spec = MetadataFieldSpec(
            key="x",
            owner=MetadataOwner.EXECUTION_COPY,
            allowed_locations=("task_execution_copy",),
        )
        assert spec.allowed_locations == ("task_execution_copy",)

    def test_allows_task_execution_copy_true(self) -> None:
        spec = MetadataFieldSpec(
            key="x",
            owner=MetadataOwner.WORK_ITEM,
            allowed_locations=("work_item", "task_execution_copy"),
        )
        assert spec.allows_task_execution_copy is True

    def test_allows_task_execution_copy_false(self) -> None:
        spec = MetadataFieldSpec(key="x", owner=MetadataOwner.WORK_ITEM)
        assert spec.allows_task_execution_copy is False

    def test_allows_task_legacy_read_fallback(self) -> None:
        spec = MetadataFieldSpec(
            key="x",
            owner=MetadataOwner.WORK_ITEM,
            legacy_fallback=True,
        )
        assert spec.allows_task_legacy_read is True

    def test_allows_task_legacy_read_explicit(self) -> None:
        spec = MetadataFieldSpec(
            key="x",
            owner=MetadataOwner.WORK_ITEM,
            allowed_locations=("work_item", "task_legacy_read"),
        )
        assert spec.allows_task_legacy_read is True


class TestFieldMatrix:
    def test_keys_unique(self) -> None:
        keys = [spec.key for spec in (*_WORK_ITEM_FIELDS,)]
        assert len(keys) == len(set(keys)), "duplicate keys in WORK_ITEM_FIELDS"

    def test_all_keys_in_specs(self) -> None:
        for key in WORK_ITEM_OWNED_KEYS | RUNTIME_TASK_OWNED_KEYS | EXECUTION_COPY_KEYS:
            assert key in METADATA_FIELD_SPECS, f"{key!r} missing from specs"

    def test_work_item_keys_non_empty(self) -> None:
        assert len(WORK_ITEM_OWNED_KEYS) > 0

    def test_execution_copy_includes_work_item_shared(self) -> None:
        """EXECUTION_COPY_KEYS 是 EXECUTION_COPY owner ∪ allows_task_execution_copy 的并集。"""
        assert WORK_ITEM_OWNED_KEYS & EXECUTION_COPY_KEYS

    def test_no_empty_key(self) -> None:
        for spec in METADATA_FIELD_SPECS.values():
            assert spec.key, "empty key in specs"
