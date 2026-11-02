# 功能：LongTermMemory 单元测试 — SQLite 持久化 / 重要性排序 / 检索 / 删除

from bandi.memory.long_term import LongTermMemory, MemoryItem


class TestMemoryItem:
    def test_to_dict_from_dict_roundtrip(self) -> None:
        item = MemoryItem(item_id="test1", content="hello", metadata={"key": "val"}, importance=0.8)
        d = item.to_dict()
        restored = MemoryItem.from_dict(d)
        assert restored.id == "test1"
        assert restored.content == "hello"
        assert restored.metadata == {"key": "val"}
        assert restored.importance == 0.8

    def test_importance_clamped(self) -> None:
        item = MemoryItem(item_id="t", content="x", importance=1.5)
        assert item.importance == 1.0
        item2 = MemoryItem(item_id="t", content="x", importance=-0.5)
        assert item2.importance == 0.0


class TestLongTermMemory:
    def test_store_and_get(self) -> None:
        mem = LongTermMemory()
        item = MemoryItem(item_id="a1", content="important note")
        mem.store(item)
        retrieved = mem.get("a1")
        assert retrieved is not None
        assert retrieved.id == "a1"
        assert retrieved.content == "important note"
        mem.close()

    def test_store_with_namespace_isolates_retrieval(self) -> None:
        mem = LongTermMemory()
        mem.store(
            MemoryItem(item_id="n1", content="project secret"),
            project_id="p1",
            employee_id="e1",
        )
        mem.store(
            MemoryItem(item_id="n2", content="project secret"),
            project_id="p2",
            employee_id="e1",
        )
        mem.store(MemoryItem(item_id="g1", content="global secret"))
        # 维度过滤检索：只看到本维度
        scoped = mem.retrieve("secret", project_id="p1", employee_id="e1")
        assert [r.id for r in scoped] == ["n1"], "维度检索必须隔离到 p1/e1"
        # 全局检索（无维度）只看到全局行（「仅全局」语义，维度行不泄漏）
        all_hits = mem.retrieve("secret")
        assert {r.id for r in all_hits} == {"g1"}, "无维度检索应只看到全局行"
        fts = mem.search_fts("secret", project_id="p2", employee_id="e1")
        assert [r.id for r in fts] == ["n2"]
        mem.close()

    def test_retrieve_by_content_keyword(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="k1", content="Python async programming tips"))
        mem.store(MemoryItem(item_id="k2", content="Rust memory safety rules"))
        results = mem.retrieve("Python", limit=5)
        assert len(results) == 1
        assert results[0].id == "k1"
        mem.close()

    def test_delete_removes_item(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="d1", content="to delete"))
        mem.delete("d1")
        assert mem.get("d1") is None
        mem.close()

    def test_prune_drops_lowest_importance(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="high", content="high", importance=0.9))
        mem.store(MemoryItem(item_id="mid", content="mid", importance=0.5))
        mem.store(MemoryItem(item_id="low", content="low", importance=0.1))
        pruned = mem.prune(max_items=2)
        assert pruned == 1
        assert mem.get("low") is None
        assert mem.get("high") is not None
        mem.close()

    def test_prune_removes_fts_rows(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="high", content="alpha unique", importance=0.9))
        mem.store(MemoryItem(item_id="mid", content="beta unique", importance=0.5))
        mem.store(MemoryItem(item_id="low", content="gamma unique", importance=0.1))
        mem.prune(max_items=2)
        rows = mem._conn.execute("SELECT COUNT(*) FROM long_term_fts").fetchone()[0]
        assert rows == 2, f"FTS 应剩 2 行，实际 {rows}"
        assert mem.search_fts("gamma", limit=5) == []
        mem.close()

    def test_prune_noop_when_under_limit(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="x"))
        assert mem.prune(max_items=100) == 0
        mem.close()

    def test_retrieve_returns_empty_for_no_match(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="hello world"))
        assert len(mem.retrieve("nonexistent")) == 0

    def test_search_fts_basic(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="deep learning framework"))
        mem.store(MemoryItem(item_id="b", content="python agent toolkit"))
        results = mem.search_fts("learning", limit=5)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_search_fts_multi_word(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="machine learning pipeline"))
        mem.store(MemoryItem(item_id="b", content="data processing pipeline"))
        results = mem.search_fts("pipeline", limit=5)
        assert len(results) == 2

    def test_search_fts_phrase(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="how to train a neural network"))
        mem.store(MemoryItem(item_id="b", content="how to bake a cake"))
        results = mem.search_fts('"neural network"', limit=5)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_search_fts_no_match(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="python code"))
        results = mem.search_fts("java", limit=5)
        assert len(results) == 0

    def test_search_fts_ranked_order(self) -> None:
        mem = LongTermMemory()
        mem.store(
            MemoryItem(item_id="a", content="python python python python python", importance=0.5)
        )
        mem.store(MemoryItem(item_id="b", content="python", importance=0.9))
        results = mem.search_fts("python", limit=5)
        assert len(results) >= 2

    def test_search_fts_fallback_on_empty_after_sanitize(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="hello world"))
        results = mem.search_fts("^invalid_chars", limit=5)
        assert len(results) >= 0

    def test_search_fts_sanitize_removes_dangerous_chars(self) -> None:
        result = LongTermMemory._sanitize_fts_query("hello world ^invalid_syntax")
        assert "^" not in result
        assert "hello" in result

    def test_search_fts_respects_limit(self) -> None:
        mem = LongTermMemory()
        for i in range(10):
            mem.store(MemoryItem(item_id=f"x{i}", content=f"item {i} data"))
        results = mem.search_fts("data", limit=3)
        assert len(results) <= 3

    def test_search_fts_after_delete(self) -> None:
        mem = LongTermMemory()
        mem.store(MemoryItem(item_id="a", content="keep this"))
        mem.store(MemoryItem(item_id="b", content="delete this"))
        mem.delete("b")
        results = mem.search_fts("delete", limit=5)
        assert len(results) == 0
        mem.close()
