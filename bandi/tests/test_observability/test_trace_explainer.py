from __future__ import annotations

import pytest
import pytest_asyncio

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.memory.session.store import SessionStore
from bandi.observability.trace_explainer import TraceExplainer


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def store() -> SessionStore:
    import os
    import tempfile

    tmp = tempfile.mktemp(suffix=".db")
    s = SessionStore(tmp)
    yield s
    s.close()
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest_asyncio.fixture
async def explainer(bus: EventBus, store: SessionStore) -> TraceExplainer:
    e = TraceExplainer(bus, store)
    await e.start()
    yield e


@pytest.mark.asyncio
async def test_empty_explain(explainer: TraceExplainer) -> None:
    result = explainer.explain()
    assert result == "（无执行轨迹）"


@pytest.mark.asyncio
async def test_single_llm_response_step(explainer: TraceExplainer, bus: EventBus) -> None:
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 1,
                "model": "test-model",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    result = explainer.explain("s1")
    assert "LLM 决策" in result
    assert "test-model" in result
    assert "in=10" in result


@pytest.mark.asyncio
async def test_tool_invoked_and_completed(explainer: TraceExplainer, bus: EventBus) -> None:
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 1,
                "model": "test-model",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    await bus.emit(
        Event(
            EventType.TOOL_INVOKED,
            {
                "session_id": "s1",
                "turn": 1,
                "tool_name": "search_entities",
                "tool_call_id": "c1",
            },
        )
    )
    await bus.emit(
        Event(
            EventType.TOOL_COMPLETED,
            {
                "session_id": "s1",
                "turn": 1,
                "tool_name": "search_entities",
                "output_preview": "找到 3 个任务",
            },
        )
    )
    result = explainer.explain("s1")
    assert "LLM 决策" in result
    assert "调用工具" in result
    assert "search_entities" in result
    assert "工具返回" in result
    assert "找到 3 个任务" in result


@pytest.mark.asyncio
async def test_multi_session_isolation(explainer: TraceExplainer, bus: EventBus) -> None:
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 1,
                "model": "model-s1",
            },
        )
    )
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s2",
                "turn": 1,
                "model": "model-s2",
            },
        )
    )
    result_s1 = explainer.explain("s1")
    result_s2 = explainer.explain("s2")
    assert "model-s1" in result_s1
    assert "model-s2" not in result_s1
    assert "model-s2" in result_s2
    assert "model-s1" not in result_s2


@pytest.mark.asyncio
async def test_multi_turn_explain(explainer: TraceExplainer, bus: EventBus) -> None:
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 1,
                "model": "model-1",
            },
        )
    )
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 2,
                "model": "model-2",
            },
        )
    )
    result = explainer.explain("s1")
    assert "[轮次1]" in result
    assert "[轮次2]" in result
    assert "model-1" in result
    assert "model-2" in result
