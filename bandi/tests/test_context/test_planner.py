from __future__ import annotations

from bandi.context.item import ContextItem
from bandi.context.planner import ContextPlanner


class TestContextPlanner:
    def test_all_items_fit(self) -> None:
        items = [
            ContextItem(id="a", source_type="t", content="x", priority=1, token_estimate=50),
            ContextItem(id="b", source_type="t", content="y", priority=5, token_estimate=30),
        ]
        result = ContextPlanner.plan(items, max_tokens=200)
        assert len(result) == 2
        assert result[0].id == "b"
        assert result[1].id == "a"

    def test_some_items_trimmed_by_budget(self) -> None:
        items = [
            ContextItem(id="a", source_type="t", content="x", priority=5, token_estimate=100),
            ContextItem(id="b", source_type="t", content="y", priority=3, token_estimate=30),
            ContextItem(id="c", source_type="t", content="z", priority=1, token_estimate=60),
        ]
        result = ContextPlanner.plan(items, max_tokens=150)
        assert len(result) == 2
        assert result[0].id == "a"
        assert result[1].id == "b"

    def test_exact_budget(self) -> None:
        items = [
            ContextItem(id="a", source_type="t", content="x", priority=1, token_estimate=100),
            ContextItem(id="b", source_type="t", content="y", priority=2, token_estimate=50),
        ]
        result = ContextPlanner.plan(items, max_tokens=150)
        assert len(result) == 2

    def test_empty_items(self) -> None:
        result = ContextPlanner.plan([], max_tokens=4096)
        assert result == []

    def test_single_item_exceeds_budget_omitted(self) -> None:
        items = [
            ContextItem(id="a", source_type="t", content="x", priority=1, token_estimate=200),
        ]
        result = ContextPlanner.plan(items, max_tokens=100)
        assert result == []

    def test_negative_budget_returns_empty(self) -> None:
        items = [
            ContextItem(id="a", source_type="t", content="x", priority=1, token_estimate=10),
        ]
        result = ContextPlanner.plan(items, max_tokens=0)
        assert result == []
