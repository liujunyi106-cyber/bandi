from __future__ import annotations

import pytest

from bandi.context.item import ContextItem
from bandi.context.source import ContextSourceRegistry, RuntimeContextSource
from bandi.core.message import Message, MessageRole


class _MockSource:
    def __init__(self, items: list[ContextItem] | None = None) -> None:
        self._items = items or []
        self.called_with: dict | None = None

    async def collect(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]:
        self.called_with = {"message": message, "session_id": session_id, "budget": budget}
        return self._items


class TestRuntimeContextSourceProtocol:
    @pytest.mark.asyncio
    async def test_protocol_duck_type(self) -> None:
        source: RuntimeContextSource = _MockSource()
        msg = Message(role=MessageRole.USER, content="hi")
        result = await source.collect(msg, "s-1")
        assert result == []


class TestContextSourceRegistry:
    def test_register_and_get(self) -> None:
        reg = ContextSourceRegistry()
        src = _MockSource()
        reg.register("test_src", src)
        assert reg.get("test_src") is src

    def test_get_missing_returns_none(self) -> None:
        reg = ContextSourceRegistry()
        assert reg.get("nonexistent") is None

    def test_all_returns_copy(self) -> None:
        reg = ContextSourceRegistry()
        src = _MockSource()
        reg.register("a", src)
        all_sources = reg.all()
        assert all_sources == {"a": src}
        all_sources["b"] = _MockSource()
        assert "b" not in reg.all()

    @pytest.mark.asyncio
    async def test_collect_all_empty_registry(self) -> None:
        reg = ContextSourceRegistry()
        msg = Message(role=MessageRole.USER, content="test")
        items = await reg.collect_all(msg, "s-1")
        assert items == []

    @pytest.mark.asyncio
    async def test_collect_all_merges_and_tags(self) -> None:
        reg = ContextSourceRegistry()
        src_a = _MockSource([ContextItem(id="1", source_type="t", content="a")])
        src_b = _MockSource([ContextItem(id="2", source_type="t", content="b")])
        reg.register("src_a", src_a)
        reg.register("src_b", src_b)
        msg = Message(role=MessageRole.USER, content="test")
        items = await reg.collect_all(msg, "s-1", budget=1024)
        assert len(items) == 2
        ids = {i.id for i in items}
        assert ids == {"1", "2"}
        for item in items:
            assert item.metadata.get("source_name") in ("src_a", "src_b")

    @pytest.mark.asyncio
    async def test_collect_all_passes_message_and_session_id(self) -> None:
        reg = ContextSourceRegistry()
        src = _MockSource()
        reg.register("s", src)
        msg = Message(role=MessageRole.USER, content="hello")
        await reg.collect_all(msg, "sid-99", budget=512)
        assert src.called_with is not None
        assert src.called_with["message"].content == "hello"
        assert src.called_with["session_id"] == "sid-99"

    @pytest.mark.asyncio
    async def test_collect_all_splits_budget(self) -> None:
        reg = ContextSourceRegistry()
        src_a = _MockSource()
        src_b = _MockSource()
        reg.register("a", src_a)
        reg.register("b", src_b)
        msg = Message(role=MessageRole.USER, content="test")
        await reg.collect_all(msg, "s-1", budget=1000)
        assert src_a.called_with["budget"] == 500
        assert src_b.called_with["budget"] == 500

    @pytest.mark.asyncio
    async def test_collect_all_handles_none_metadata(self) -> None:
        reg = ContextSourceRegistry()

        class _BadSource:
            async def collect(
                self,
                message: Message,
                session_id: str,
                budget: int = 4096,
                intent_hint: str | None = None,
            ) -> list[ContextItem]:
                item = ContextItem(id="bad", source_type="t", content="x")
                item.metadata = None  # type: ignore[assignment]
                return [item]

        reg.register("bad", _BadSource())
        msg = Message(role=MessageRole.USER, content="test")
        items = await reg.collect_all(msg, "s-1")
        assert len(items) == 1
        assert items[0].metadata == {"source_name": "bad"}
