from __future__ import annotations

from bandi.context.item import ContextItem


class TestContextItem:
    def test_create_with_minimal_fields(self) -> None:
        item = ContextItem(id="ctx-1", source_type="user", content="hello")
        assert item.id == "ctx-1"
        assert item.source_type == "user"
        assert item.content == "hello"
        assert item.priority == 0
        assert item.token_estimate == 0
        assert item.metadata == {}

    def test_create_with_all_fields(self) -> None:
        item = ContextItem(
            id="ctx-2",
            source_type="entity",
            content="@项目",
            priority=5,
            token_estimate=10,
            metadata={"entity_id": "proj-1"},
        )
        assert item.priority == 5
        assert item.token_estimate == 10
        assert item.metadata["entity_id"] == "proj-1"

    def test_metadata_mutable(self) -> None:
        item = ContextItem(id="ctx-3", source_type="session", content="hi")
        item.metadata["role"] = "user"
        assert item.metadata["role"] == "user"

    def test_priority_ordering(self) -> None:
        low = ContextItem(id="a", source_type="t", content="", priority=1)
        high = ContextItem(id="b", source_type="t", content="", priority=10)
        assert high.priority > low.priority
