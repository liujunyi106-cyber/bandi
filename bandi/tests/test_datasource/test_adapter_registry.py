import pytest

from bandi.datasource.core import AdapterRegistry, DataSourceAdapter, SchemaSummary, TableResult


class _FakeAdapter(DataSourceAdapter):
    def __init__(self, config: dict) -> None:
        self.config = config

    async def inspect_schema(self, input) -> SchemaSummary:
        return SchemaSummary()

    async def preview_table(self, input) -> TableResult:
        return TableResult()

    async def run_sql_readonly(self, input) -> TableResult:
        return TableResult()


class TestAdapterRegistry:
    def setup_method(self) -> None:
        AdapterRegistry.clear()

    def test_register_and_create(self) -> None:
        AdapterRegistry.register("fake", lambda c: _FakeAdapter(c))
        adapter = AdapterRegistry.create("fake", {"key": "val"})
        assert isinstance(adapter, _FakeAdapter)
        assert adapter.config == {"key": "val"}

    def test_create_unsupported_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported data source type: nonexistent"):
            AdapterRegistry.create("nonexistent", {})

    def test_register_duplicate_overwrites(self) -> None:
        AdapterRegistry.register("dup", lambda c: _FakeAdapter(c))
        AdapterRegistry.register("dup", lambda c: _FakeAdapter({"override": True}))
        adapter = AdapterRegistry.create("dup", {})
        assert adapter.config == {"override": True}

    def test_list_types(self) -> None:
        AdapterRegistry.register("a", lambda c: _FakeAdapter(c))
        AdapterRegistry.register("b", lambda c: _FakeAdapter(c))
        types = AdapterRegistry.list_types()
        assert "a" in types
        assert "b" in types
