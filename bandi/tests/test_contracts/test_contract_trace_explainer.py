from __future__ import annotations

import tempfile

import pytest

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.session.store import SessionStore
from bandi.observability.trace_explainer import TraceExplainer


@pytest.fixture
def store() -> SessionStore:
    s = SessionStore(tempfile.mktemp(suffix=".db"))
    yield s
    s.close()


@pytest.fixture
def populated_store(store: SessionStore) -> SessionStore:
    store.create_session(session_id="s1")
    store.append_message(
        "s1",
        Message(role=MessageRole.USER, content="查一下进度"),
    )
    store.append_message(
        "s1",
        Message(
            role=MessageRole.ASSISTANT,
            content="已完成",
            tool_calls=[ToolCall(id="c1", name="search_entities", arguments={"q": "进度"})],
        ),
    )
    return store


def test_contract_explain_rebuilds_persistent_trace_when_memory_empty(
    populated_store: SessionStore,
) -> None:
    # 只读观测模式（event_bus=None）：内存恒空，必须从持久化重建
    explainer = TraceExplainer(event_bus=None, session_store=populated_store)
    steps = explainer.get_steps("s1")
    assert [s.action for s in steps] == ["user_asked", "tool_called", "llm_decided"]
    assert steps[1].detail == "search_entities({'q': '进度'})"
    explanation = explainer.explain("s1")
    assert "用户提问" in explanation
    assert "调用工具" in explanation
    assert "LLM 决策" in explanation


@pytest.mark.asyncio
async def test_contract_explain_prefers_memory_over_store(store: SessionStore) -> None:
    # 事件内存信息更全（含 result_observed/usage），有内存时不得被 store 数据覆盖
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType

    store.create_session(session_id="s1")
    store.append_message("s1", Message(role=MessageRole.USER, content="来自持久化"))

    bus = EventBus()
    explainer = TraceExplainer(bus, store)
    await explainer.start()
    await bus.emit(
        Event(
            EventType.LLM_RESPONSE,
            {
                "session_id": "s1",
                "turn": 1,
                "provider": "dp",
                "model": "x",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    explanation = explainer.explain("s1")
    assert "dp/x" in explanation
    assert "来自持久化" not in explanation


def test_contract_explain_swallows_bad_at_cutoff(store: SessionStore) -> None:
    # 非法 at 截断点：explainer 不得抛异常，按空轨迹处理（端点另行 404 门禁）
    store.create_session(session_id="s1")
    store.append_message("s1", Message(role=MessageRole.USER, content="一条"))
    explainer = TraceExplainer(event_bus=None, session_store=store)
    assert explainer.explain("s1", at="no-such-entry") == "（无执行轨迹）"


def test_contract_explain_no_trace_returns_sentinel(store: SessionStore) -> None:
    explainer = TraceExplainer(event_bus=None, session_store=store)
    assert explainer.explain("empty") == "（无执行轨迹）"
    assert explainer.get_steps("empty") == []
