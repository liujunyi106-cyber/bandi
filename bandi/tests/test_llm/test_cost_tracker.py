import pytest

from bandi.llm.cost_tracker import CostEntry, CostTracker, SessionCost
from bandi.llm.model_catalog import ModelCatalog


@pytest.fixture
def catalog():
    return ModelCatalog()


@pytest.fixture
def tracker(catalog):
    return CostTracker(catalog, budget_limit=1.0)


def test_record_basic(tracker):
    entry = tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=500,
    )
    assert entry.provider == "deepseek"
    assert entry.model == "deepseek-v4-flash"
    assert entry.prompt_tokens == 1000
    assert entry.completion_tokens == 500
    assert entry.cost_cny > 0


def test_cost_calculation(tracker):
    entry = tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    assert entry.cost_cny == pytest.approx(1.584, rel=0.01)


def test_daily_total(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=500_000,
        completion_tokens=0,
    )
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=500_000,
        completion_tokens=0,
    )
    assert tracker.daily_total == pytest.approx(1.584, rel=0.01)


def test_entries_list(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=100,
        completion_tokens=50,
    )
    entries = tracker.entries
    assert len(entries) == 1
    assert isinstance(entries[0], CostEntry)


def test_check_budget_no_limit(tracker):
    tracker._budget_limit = -1.0
    assert tracker.check_budget() is True


def test_check_budget_within_limit(tracker):
    tracker.record(
        session_id="s1", provider="mock", model="mock", prompt_tokens=100, completion_tokens=50
    )
    assert tracker.check_budget() is True


def test_check_budget_exceeded(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=2_000_000,
        completion_tokens=0,
    )
    assert tracker.check_budget() is False


def test_get_session_cost(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=500,
    )
    sc = tracker.get_session_cost("s1")
    assert isinstance(sc, SessionCost)
    assert sc.session_id == "s1"
    assert sc.total_tokens == 1500
    assert sc.token_breakdown["prompt"] == 1000
    assert sc.token_breakdown["completion"] == 500


def test_get_session_cost_empty(tracker):
    sc = tracker.get_session_cost("nonexistent")
    assert sc.total_tokens == 0
    assert sc.total_cost_cny == 0.0


class TestToolStats:
    @pytest.mark.asyncio
    async def test_on_tool_done_records_stats(self):
        tracker = CostTracker(ModelCatalog())
        await tracker.on_tool_done(
            type(
                "E",
                (),
                {"data": {"tool_name": "bash", "duration_ms": 120, "success": True}},
            )()
        )
        await tracker.on_tool_done(
            type(
                "E",
                (),
                {"data": {"tool_name": "bash", "duration_ms": 30, "success": False}},
            )()
        )
        stats = tracker.tool_stats()
        assert stats["bash"]["calls"] == 2
        assert stats["bash"]["failures"] == 1
        assert stats["bash"]["total_ms"] == 150

    @pytest.mark.asyncio
    async def test_on_tool_done_empty_event_ignored(self):
        tracker = CostTracker(ModelCatalog())
        await tracker.on_tool_done(type("E", (), {"data": {}})())
        assert tracker.tool_stats() == {}


def test_get_session_cost_multiple_sessions(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=100,
        completion_tokens=0,
    )
    tracker.record(
        session_id="s2",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=200,
        completion_tokens=0,
    )
    sc = tracker.get_session_cost("s1")
    assert sc.total_tokens == 100
    sc = tracker.get_session_cost("s2")
    assert sc.total_tokens == 200


def test_reset_daily(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=0,
    )
    assert len(tracker.entries) == 1
    assert tracker.daily_total > 0
    tracker.reset_daily()
    assert len(tracker.entries) == 0
    assert tracker.daily_total == 0.0


def test_cross_day_auto_reset(tracker):
    import time

    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    before = tracker.daily_total
    assert before > 0
    tracker._entries[-1].timestamp = time.time() - 86400 * 2
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=500_000,
        completion_tokens=0,
    )
    assert tracker.daily_total < before


def test_unknown_model_cost(tracker):
    """R-6语义反转：目录外模型**不再编造金额**（原按 _DEFAULT_*_PRICE
    给出一个假数字）——展示为未定价 None；但预算仍按下限估计扣减（防换名绕过）。
    """
    entry = tracker.record(
        session_id="s1",
        provider="unknown",
        model="unknown-model",
        prompt_tokens=1000,
        completion_tokens=0,
    )
    assert entry.cost_cny is None, "未定价模型不得编造金额"
    assert tracker.daily_total > 0.0, "预算仍须扣减（防换名绕过目录）"


def test_session_cost_per_model(tracker):
    tracker.record(
        session_id="s1",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=500,
        completion_tokens=0,
    )
    tracker.record(
        session_id="s1", provider="openai", model="gpt-4o", prompt_tokens=300, completion_tokens=0
    )
    sc = tracker.get_session_cost("s1")
    assert "deepseek-v4-flash" in sc.per_model
    assert "gpt-4o" in sc.per_model
    assert sc.per_model["deepseek-v4-flash"]["tokens"] == 500
    assert sc.per_model["gpt-4o"]["tokens"] == 300


class TestToolTokenStats:
    """per-tool token 归因测试。"""

    @pytest.mark.asyncio
    async def test_tool_token_stats_attributes_tokens(self):
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        tracker = CostTracker(ModelCatalog())
        await tracker.on_tool_done(
            type(
                "Event",
                (),
                {
                    "data": {
                        "tool_name": "bash",
                        "tool_call_id": "tc-1",
                        "session_id": "s1",
                        "duration_ms": 100,
                        "success": True,
                    }
                },
            )()
        )
        await tracker.on_tool_done(
            type(
                "Event",
                (),
                {
                    "data": {
                        "tool_name": "read_file",
                        "tool_call_id": "tc-2",
                        "session_id": "s1",
                        "duration_ms": 50,
                        "success": True,
                    }
                },
            )()
        )
        await tracker.on_llm_response(
            type(
                "Event",
                (),
                {
                    "data": {
                        "session_id": "s1",
                        "provider": "ds",
                        "model": "v4-flash",
                        "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                    }
                },
            )()
        )
        stats = tracker.tool_token_stats()
        assert stats["read_file"]["prompt_tokens"] == 500
        assert stats["read_file"]["completion_tokens"] == 100
        assert stats["bash"]["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_tool_token_stats_multiple_calls_same_tool(self):
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        tracker = CostTracker(ModelCatalog())
        await tracker.on_tool_done(
            type(
                "Event",
                (),
                {
                    "data": {
                        "tool_name": "bash",
                        "tool_call_id": "tc-1",
                        "session_id": "s1",
                        "duration_ms": 10,
                        "success": True,
                    }
                },
            )()
        )
        await tracker.on_llm_response(
            type(
                "Event",
                (),
                {
                    "data": {
                        "session_id": "s1",
                        "provider": "ds",
                        "model": "v4-flash",
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    }
                },
            )()
        )
        await tracker.on_tool_done(
            type(
                "Event",
                (),
                {
                    "data": {
                        "tool_name": "bash",
                        "tool_call_id": "tc-2",
                        "session_id": "s1",
                        "duration_ms": 20,
                        "success": True,
                    }
                },
            )()
        )
        await tracker.on_llm_response(
            type(
                "Event",
                (),
                {
                    "data": {
                        "session_id": "s1",
                        "provider": "ds",
                        "model": "v4-flash",
                        "usage": {"prompt_tokens": 300, "completion_tokens": 50},
                    }
                },
            )()
        )
        stats = tracker.tool_token_stats()
        assert stats["bash"]["calls"] == 2
        assert stats["bash"]["prompt_tokens"] == 400
        assert stats["bash"]["completion_tokens"] == 70

    def test_tool_token_stats_empty(self):
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        tracker = CostTracker(ModelCatalog())
        assert tracker.tool_token_stats() == {}

    @pytest.mark.asyncio
    async def test_tool_token_stats_attribution_uses_insertion_order(self):
        """
        而非 max(keys) 字典序——tc-9 先于 tc-10 完成，应归因 tc-10。"""
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        tracker = CostTracker(ModelCatalog())
        for call_id, name in [("tc-9", "bash"), ("tc-10", "read_file")]:
            await tracker.on_tool_done(
                type(
                    "Event",
                    (),
                    {
                        "data": {
                            "tool_name": name,
                            "tool_call_id": call_id,
                            "session_id": "s1",
                            "duration_ms": 10,
                            "success": True,
                        }
                    },
                )()
            )
        await tracker.on_llm_response(
            type(
                "Event",
                (),
                {
                    "data": {
                        "session_id": "s1",
                        "provider": "ds",
                        "model": "v4-flash",
                        "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                    }
                },
            )()
        )
        stats = tracker.tool_token_stats()
        assert stats["read_file"]["prompt_tokens"] == 500
        assert stats["bash"]["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_tool_token_stats_no_residue_across_turns(self):
        """
        响应错配消费。单轮两个工具完成后，LLM 响应应只归因最近一个
        且不残留到下一轮（第二轮无新工具完成时不得再归因）。"""
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        tracker = CostTracker(ModelCatalog())
        for call_id, name in [("tc-1", "bash"), ("tc-2", "read_file")]:
            await tracker.on_tool_done(
                type(
                    "Event",
                    (),
                    {
                        "data": {
                            "tool_name": name,
                            "tool_call_id": call_id,
                            "session_id": "s1",
                            "duration_ms": 10,
                            "success": True,
                        }
                    },
                )()
            )
        await tracker.on_llm_response(
            type(
                "Event",
                (),
                {
                    "data": {
                        "session_id": "s1",
                        "provider": "ds",
                        "model": "v4-flash",
                        "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                    }
                },
            )()
        )
        assert tracker._pending_tool_calls == {}, "消费后不得残留 pending"
        stats = tracker.tool_token_stats()
        assert stats["read_file"]["prompt_tokens"] == 500
        assert stats["bash"]["prompt_tokens"] == 0


def test_tool_stats_attribution_session_isolated(tracker):
    """
    A 的 LLM 响应只归因 A 的工具调用且不清掉 B 的 pending（交错场景不互抢）。"""
    import asyncio

    class _Evt:
        def __init__(self, data):
            self.data = data

    asyncio.run(
        tracker.on_tool_done(_Evt({"tool_call_id": "tc-a", "session_id": "A", "tool_name": "bash"}))
    )
    asyncio.run(
        tracker.on_tool_done(_Evt({"tool_call_id": "tc-b", "session_id": "B", "tool_name": "read"}))
    )
    assert tracker._pending_tool_calls == {"tc-a": "A", "tc-b": "B"}

    # 走生产链路：LLM_RESPONSE 事件 → on_llm_response → record + 归因
    asyncio.run(
        tracker.on_llm_response(
            _Evt(
                {
                    "session_id": "A",
                    "provider": "p",
                    "model": "m",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            )
        )
    )
    entry_a = tracker._entries[-1]
    assert entry_a.meta.get("attributed_tool_call_id") == "tc-a", f"A 归因错配: {entry_a.meta}"

    asyncio.run(
        tracker.on_llm_response(
            _Evt(
                {
                    "session_id": "B",
                    "provider": "p",
                    "model": "m",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            )
        )
    )
    entry_b = tracker._entries[-1]
    assert entry_b.meta.get("attributed_tool_call_id") == "tc-b", (
        f"B 的 pending 被 A 清掉导致归因丢失: {entry_b.meta}"
    )
