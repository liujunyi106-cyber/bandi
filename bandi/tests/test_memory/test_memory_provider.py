import sqlite3

import pytest

from bandi.memory.long_term import LongTermMemory
from bandi.memory.long_term import MemoryItem as LTMemoryItem
from bandi.memory.memory_provider import MemoryHit, MemoryManager


@pytest.fixture
def manager() -> MemoryManager:
    return MemoryManager(
        long_term=LongTermMemory(),
    )


class TestMemoryManager:
    def test_search_all_contains_long_term(self, manager: MemoryManager) -> None:
        manager.long_term.store(
            LTMemoryItem(item_id="lt1", content="long term data", importance=0.8)
        )
        results = manager.search_all("data", limit=5)
        assert set(results.keys()) == {"long_term"}, "search_all 只保留 long_term 分类"

    def test_search_all_long_term_results(self, manager: MemoryManager) -> None:
        manager.long_term.store(
            LTMemoryItem(item_id="lt1", content="important project info", importance=0.9)
        )
        results = manager.search_all("project", limit=5)
        assert len(results["long_term"]) == 1
        assert results["long_term"][0].key == "lt1"
        assert "important" in results["long_term"][0].value

    def test_search_all_empty_query(self, manager: MemoryManager) -> None:
        results = manager.search_all("", limit=5)
        assert all(len(v) == 0 for v in results.values())

    def test_total_count_long_term_only(self, manager: MemoryManager) -> None:
        assert manager.total_count() == 0
        manager.long_term.store(LTMemoryItem(item_id="a", content="x"))
        assert manager.total_count() == 1

    def test_memory_item_dataclass(self) -> None:
        item = MemoryHit(key="k", value="v", metadata={"score": 1})
        assert item.key == "k"
        assert item.value == "v"
        assert item.metadata == {"score": 1}

    def test_memory_item_defaults(self) -> None:
        item = MemoryHit(value="only value")
        assert item.key == ""
        assert item.metadata == {}

    def test_search_all_respects_limit(self, manager: MemoryManager) -> None:
        for i in range(10):
            manager.long_term.store(
                LTMemoryItem(item_id=f"lt{i}", content=f"item {i} content", importance=0.5)
            )
        results = manager.search_all("content", limit=3)
        assert len(results["long_term"]) <= 3

    def test_close_propagates_to_sqlite_stores(self) -> None:
        """
        防每次 run 泄漏连接。"""
        lt = LongTermMemory()
        mm = MemoryManager(long_term=lt)
        mm.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            lt._conn.execute("SELECT 1")
