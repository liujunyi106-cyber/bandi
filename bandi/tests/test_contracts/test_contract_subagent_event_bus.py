# 功能：子 Agent 事件总线继承契约——父总线复用（生产装配链 context→manager→子 loop）
# 且真实 ToolExecutor 端到端可达（DoD-11/12：禁只测手工传参）。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from bandi.agent.subagent_tool import SubAgentTool
from bandi.core.event_bus import EventBus, bootstrap_event_bus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, TokenUsage, ToolCall
from bandi.llm.cost_tracker import CostTracker
from bandi.llm.mock import MockProvider
from bandi.llm.model_catalog import ModelCatalog
from bandi.tools.base import ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


def _provider() -> MockProvider:
    """功能：确定性单轮直答 provider（子 agent 无工具轮，一次 LLM 调用即完成）。"""
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="subagent done",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )
    )
    return provider


async def _collect(bus: EventBus) -> list[Event]:
    """功能：订阅子 agent 主事件类型，返回收集列表（引用共享）。"""
    events: list[Event] = []

    async def handler(event: Event) -> None:
        events.append(event)

    for event_type in (EventType.STATE_CHANGED, EventType.TERMINATED, EventType.LLM_RESPONSE):
        await bus.subscribe(event_type, handler)
    return events


@dataclass
class _Deps:
    """功能：bootstrap_event_bus 装配用最小依赖（duck-type，与生产 Dependencies 同形）。"""

    cost_tracker: Any = None
    session_store: Any = None
    logger: Any = None
    trace_explainer: Any = None


class TestContextBusPropagation:
    @pytest.mark.asyncio
    async def test_subagent_tool_propagates_context_event_bus(self) -> None:
        """SubAgentTool 必须把 context.event_bus 交给 manager——子 agent 事件
        （source=subagent）到达父总线，而非落入自建总线。"""
        bus = EventBus()
        events = await _collect(bus)
        tool = SubAgentTool(provider=_provider())
        out = await tool.execute({"task": "do it"}, context=ToolPermissionContext(event_bus=bus))
        assert out.get("success"), out
        assert events, "子 agent 事件必须到达父总线"
        assert any(e.source == "subagent" for e in events), (
            "子 agent 事件应带 source=subagent 归属标记（trace/成本可区分）"
        )

    @pytest.mark.asyncio
    async def test_no_bus_context_still_runs(self) -> None:
        """无 event_bus（旧调用方/库用法）降级自建总线——执行不阻断。"""
        tool = SubAgentTool(provider=_provider())
        out = await tool.execute({"task": "do it"}, context=ToolPermissionContext())
        assert out.get("success"), out


class TestProductionChain:
    @pytest.mark.asyncio
    async def test_executor_injects_bus_into_tool_context(self) -> None:
        """生产链（DoD-12）：真实 ToolExecutor(event_bus=bus) → 子 agent 事件到 bus。

        不 mock 中间层——走 executor._run_and_record 的真实 context 构造与透传。
        """
        bus = EventBus()
        events = await _collect(bus)
        registry = ToolRegistry()
        registry.register_instance(SubAgentTool(provider=_provider()))
        executor = ToolExecutor(registry, event_bus=bus)
        results = await executor.execute(
            [ToolCall(id="c1", name="subagent_spawn", arguments={"task": "do it"})]
        )
        assert results and results[0].success, results
        assert any(e.source == "subagent" for e in events), (
            "子 agent 事件必须经父 executor 的 bus 到达（否则成本/trace 断裂）"
        )

    @pytest.mark.asyncio
    async def test_subagent_tokens_land_in_shared_cost_tracker(self) -> None:
        """9.3 端到端：子 agent 的 LLM_RESPONSE 入共享台账——按子会话可查 token。

        生产装配（bootstrap_event_bus 订阅 cost_tracker）→ 真实 executor → 子 agent。
        """
        bus = EventBus()
        events = await _collect(bus)
        tracker = CostTracker(catalog=ModelCatalog(), budget_limit=-1)
        await bootstrap_event_bus(bus, _Deps(cost_tracker=tracker))
        registry = ToolRegistry()
        registry.register_instance(SubAgentTool(provider=_provider()))
        executor = ToolExecutor(registry, event_bus=bus)
        results = await executor.execute(
            [ToolCall(id="c1", name="subagent_spawn", arguments={"task": "do it"})]
        )
        assert results and results[0].success, results
        sub_sessions = {e.session_id for e in events if e.source == "subagent" and e.session_id}
        assert sub_sessions, "子 agent 事件必须带子会话 id 归属"
        total = sum(tracker.get_session_cost(sid).total_tokens for sid in sub_sessions)
        assert total > 0, "子 agent token 必须记入共享台账（此前断裂）"
