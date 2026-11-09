"""
契约族：事件 handler 消费幂等（七-）。

背景：EventBus.emit 用 asyncio.gather 并行通知全部订阅者（cost_tracker /
session_store / logger / MetricsBridge / OTel）。事件重放或重复 emit 时
无幂等约束的 handler 会重复写入（成本重复计费、状态重复写入、metrics 翻倍）。
审计建议：订阅者接收 event_id 并自行维护消费 offset/idempotency。

本契约钉的行为族（生产调用路径）：
- `llm/cost_tracker.py::CostTracker.on_llm_response`（LLM_RESPONSE 订阅者）
- `llm/cost_tracker.py::CostTracker.on_tool_done`（TOOL_COMPLETED 订阅者）
- `core/event_bus.py::EventBus.emit`（gather 并行通知）

断言「最终产物」：同一 event_id 事件重复消费（同对象重复 emit 或 replay）
成本 tracker 必须只记录一次——entries 不翻倍、当日总额不翻倍。
"""

from __future__ import annotations

import asyncio

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.llm.cost_tracker import CostTracker
from bandi.llm.model_catalog import ModelCatalog


def _tracker() -> CostTracker:
    catalog = ModelCatalog()  # 空目录，record 走 provider 计算（无匹配模型 cost=0 但仍记 entry）
    return CostTracker(catalog=catalog, budget_limit=-1)


async def _emit(bus: EventBus, ev: Event) -> None:
    await bus.emit(ev)


def test_contract_cost_tracker_idempotent_on_duplicate_llm_response() -> None:
    """同一 event_id 的 LLM_RESPONSE 重复 emit：cost_tracker 必须只记一次（防重复计费）。"""
    tracker = _tracker()
    bus = EventBus()
    asyncio.run(bus.subscribe(EventType.LLM_RESPONSE, tracker.on_llm_response))

    ev = Event(
        type=EventType.LLM_RESPONSE,
        data={
            "provider": "openai",
            "model": "gpt-4o",
            "session_id": "s1",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )
    asyncio.run(_emit(bus, ev))
    asyncio.run(_emit(bus, ev))  # 同 event_id 重复 emit（重放场景）
    assert len(tracker.entries) == 1, (
        f"同 event_id 重复消费必须只记一次，entries={len(tracker.entries)}"
    )


def test_contract_cost_tracker_idempotent_on_duplicate_tool_done() -> None:
    """同一 event_id 的 TOOL_COMPLETED 重复 emit：工具统计必须只记一次。"""
    tracker = _tracker()
    bus = EventBus()
    asyncio.run(bus.subscribe(EventType.TOOL_COMPLETED, tracker.on_tool_done))

    ev = Event(
        type=EventType.TOOL_COMPLETED,
        data={"tool_name": "bash", "duration_ms": 12.0, "success": True},
    )
    asyncio.run(_emit(bus, ev))
    asyncio.run(_emit(bus, ev))
    stats = tracker.tool_stats(limit=10)
    assert stats.get("bash", {}).get("calls", 0) == 1, (
        f"同 event_id 工具统计必须只记一次，stats={stats}"
    )


def test_contract_cost_tracker_distinct_events_both_recorded() -> None:
    """不同 event_id 的同类事件必须各自记录（幂等不得误杀新事件）。"""
    tracker = _tracker()
    bus = EventBus()
    asyncio.run(bus.subscribe(EventType.LLM_RESPONSE, tracker.on_llm_response))
    for i in range(3):
        asyncio.run(
            _emit(
                bus,
                Event(
                    type=EventType.LLM_RESPONSE,
                    data={
                        "provider": "openai",
                        "model": "gpt-4o",
                        "session_id": "s1",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    },
                ),
            )
        )
    assert len(tracker.entries) == 3, f"不同事件必须各记一次，entries={len(tracker.entries)}"


def test_contract_event_bus_skips_handlers_on_duplicate_event_id() -> None:
    """EventBus 层统一去重（七-覆盖全部订阅者）：同 event_id 重复 emit
    不得重复通知任何 handler（MetricsBridge/OTel/未来订阅者自动幂等）。"""
    calls: list[str] = []
    bus = EventBus()
    captured: list[Event] = []

    async def spy(event: Event) -> None:
        calls.append(event.event_id)
        captured.append(event)

    asyncio.run(bus.subscribe(EventType.LLM_REQUEST, spy))
    ev = Event(type=EventType.LLM_REQUEST, data={"x": 1})
    asyncio.run(_emit(bus, ev))
    asyncio.run(_emit(bus, ev))  # 同 event_id 重复 emit
    assert len(calls) == 1, f"重复 event_id 不得重复通知 handler，calls={len(calls)}"


def test_contract_event_bus_dedup_works_without_persistence() -> None:
    """未启用持久化的 EventBus 也必须去重（进程内去重与持久化解耦）。"""
    calls: list[str] = []
    bus = EventBus()  # 无 persist_path

    async def spy2(event: Event) -> None:
        calls.append(event.event_id)

    asyncio.run(bus.subscribe(EventType.TOOL_COMPLETED, spy2))
    ev = Event(type=EventType.TOOL_COMPLETED, data={"t": "bash"})
    asyncio.run(_emit(bus, ev))
    asyncio.run(_emit(bus, ev))
    assert len(calls) == 1, f"无持久化也必须去重，calls={len(calls)}"
