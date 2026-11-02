# 功能：GlobalMemoryStore 测试（event 事件溯源 + global_memory 表）

import pytest

from bandi.memory.global_store import GlobalMemoryStore


@pytest.fixture
def store(tmp_path) -> GlobalMemoryStore:
    return GlobalMemoryStore(tmp_path / "memory.db")


class TestEventSourcing:
    def test_append_and_list_events(self, store: GlobalMemoryStore) -> None:
        e1 = store.append_event("AGENT_TURN", {"summary": "完成选品分析"})
        e2 = store.append_event("AGENT_TURN", {"summary": "生成竞品报告"})
        events = store.list_events()
        assert len(events) == 2
        assert events[0]["seq"] == e1
        assert events[1]["seq"] == e2
        assert events[0]["event_type"] == "AGENT_TURN"
        assert events[0]["payload"]["summary"] == "完成选品分析"

    def test_seq_is_monotonic(self, store: GlobalMemoryStore) -> None:
        seqs = [store.append_event("E", {"n": i}) for i in range(5)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 5

    def test_query_events_by_type(self, store: GlobalMemoryStore) -> None:
        store.append_event("AGENT_TURN", {"summary": "a"})
        store.append_event("TOOL_CALL", {"tool": "bash"})
        store.append_event("AGENT_TURN", {"summary": "b"})
        turns = store.query_events(event_type="AGENT_TURN")
        assert len(turns) == 2
        assert all(e["event_type"] == "AGENT_TURN" for e in turns)

    def test_events_persist_across_reopen(self, tmp_path) -> None:
        path = tmp_path / "memory.db"
        GlobalMemoryStore(path).append_event("E", {"k": "v"})
        reopened = GlobalMemoryStore(path)
        assert len(reopened.list_events()) == 1
        assert reopened.list_events()[0]["payload"]["k"] == "v"

    def test_empty_store_returns_empty_list(self, store: GlobalMemoryStore) -> None:
        assert store.list_events() == []

    def test_append_with_event_id_is_idempotent(self, store: GlobalMemoryStore) -> None:
        seq1 = store.append_event("AGENT_TURN", {"summary": "a"}, event_id="evt_001")
        seq2 = store.append_event("AGENT_TURN", {"summary": "b"}, event_id="evt_001")
        assert seq1 == seq2
        assert len(store.list_events()) == 1
        assert store.list_events()[0]["payload"]["summary"] == "a"

    def test_append_without_event_id_keeps_legacy_seq(self, store: GlobalMemoryStore) -> None:
        seq = store.append_event("AGENT_TURN", {"summary": "a"})
        assert seq >= 1
        assert len(store.list_events()) == 1


class TestGlobalMemory:
    def test_put_and_get(self, store: GlobalMemoryStore) -> None:
        store.put_global_memory("品牌定位", "数字员工组织向，非数据中台")
        assert store.get("品牌定位") == "数字员工组织向，非数据中台"

    def test_upsert_overwrites(self, store: GlobalMemoryStore) -> None:
        store.put_global_memory("k", "v1")
        store.put_global_memory("k", "v2")
        assert store.get("k") == "v2"
        assert store.count() == 1

    def test_search_like(self, store: GlobalMemoryStore) -> None:
        store.put_global_memory("产品定位", "个人多 Agent 数字员工组织")
        store.put_global_memory("技术栈", "React 18 + shadcn/ui")
        hits = store.search("Agent")
        assert [h["key"] for h in hits] == ["产品定位"]

    def test_missing_key_returns_none(self, store: GlobalMemoryStore) -> None:
        assert store.get("不存在") is None

    def test_global_memory_persists_across_reopen(self, tmp_path) -> None:
        path = tmp_path / "memory.db"
        GlobalMemoryStore(path).put_global_memory("k", "持久值")
        reopened = GlobalMemoryStore(path)
        assert reopened.get("k") == "持久值"

    def test_json_roundtrip_value(self, store: GlobalMemoryStore) -> None:
        value = {"a": 1, "b": ["x", "y"]}
        store.put_global_memory("复杂", value)
        assert store.get("复杂") == value


class TestClamping:
    def test_global_memory_value_clamped_to_max_chars(self, store: GlobalMemoryStore) -> None:
        store.put_global_memory("long", "x" * 8000)
        stored = store.get("long")
        assert isinstance(stored, str)
        assert len(stored) <= 4000

    def test_short_string_roundtrips_unclamped(self, store: GlobalMemoryStore) -> None:
        value = "中文短值"
        store.put_global_memory("k", value)
        assert store.get("k") == value

    def test_payload_json_roundtrip(self, store: GlobalMemoryStore) -> None:
        store.append_event("EVENT", {"数据": "中文内容"})
        event = store.list_events()[0]
        assert event["payload"]["数据"] == "中文内容"

    def test_oversized_with_quotes_roundtrips_valid(self, store: GlobalMemoryStore) -> None:
        """超长含引号/换行内容必须存为合法 JSON，读回不得是截断损坏文本。"""
        content = 'x"y\\z\n' * 900  # 引号+反斜杠+换行，dumps 后远超 4000
        store.put_global_memory("quoted", content)
        stored = store.get("quoted")
        assert isinstance(stored, str), f"必须读回字符串而非损坏 JSON 文本: {stored!r}"
        assert stored.startswith('x"y\\z\n'), f"内容前缀必须保留: {stored[:20]!r}"
        assert not stored.startswith('"'), "读回值不得是未解析的 JSON 原文（含前引号）"

    def test_oversized_non_string_rejected(self, store: GlobalMemoryStore) -> None:
        """非字符串超限不得 str() 破坏类型（读回 dict→str）——应拒绝写入。"""
        with pytest.raises(ValueError, match="cannot be truncated"):
            store.put_global_memory("big", {"data": "x" * 9000})
