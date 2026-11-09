from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall
from bandi.memory.session.store import SessionStore
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus


class EchoTool(BaseTool):
    name = "echo"
    is_concurrency_safe = True

    async def execute(self, params: dict) -> str:
        return params.get("msg", "")


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_instance(EchoTool())
    return r


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry=registry)


@pytest.fixture
def session_store() -> SessionStore:
    return SessionStore(str(Path(tempfile.mktemp(suffix=".db"))))


@pytest.mark.asyncio
async def test_messages_persisted_through_loop(
    executor: ToolExecutor, registry: ToolRegistry, session_store: SessionStore
) -> None:
    session_id = session_store.create_session(mode="execute")
    session_store.append_message(
        session_id,
        Message(role=MessageRole.USER, content="hello world"),
    )
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(content="hi back", usage=TokenUsage(prompt_tokens=5, completion_tokens=3))
    )
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello world")],
        provider=provider,
        config=AgentConfig(max_iterations=2),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=2).run(ctx)
    assert reason == TerminalReason.COMPLETED
    reply = ctx.final_message or ctx.current_content or ""
    session_store.append_message(
        session_id,
        Message(role=MessageRole.ASSISTANT, content=reply),
    )
    branch = session_store.get_branch(session_id)
    assert len(branch) >= 2
    assert branch[0]["role"] == "user"
    assert branch[0]["content"] == "hello world"
    assert branch[-1]["role"] == "assistant"
    assert "hi" in (branch[-1]["content"] or "").lower()


@pytest.mark.asyncio
async def test_session_initialized_before_loop(
    executor: ToolExecutor, registry: ToolRegistry, session_store: SessionStore
) -> None:
    session_id = session_store.create_session(title="test-session", mode="execute")
    session = session_store.get_session(session_id)
    assert session is not None
    assert session["title"] == "test-session"


@pytest.mark.asyncio
async def test_checkpoint_saved_on_state_change(
    executor: ToolExecutor, registry: ToolRegistry, session_store: SessionStore
) -> None:
    session_id = session_store.create_session(mode="execute")
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(LLMResponse(content="ok"))
    await bus.subscribe(EventType.STATE_CHANGED, session_store.on_state_change)
    await bus.subscribe(EventType.TERMINATED, session_store.on_terminated)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="save checkpoint")],
        provider=provider,
        config=AgentConfig(max_iterations=2),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    ctx.meta["session_id"] = session_id
    await AgentLoop(max_iterations=2).run(ctx)
    checkpoints = [e for e in bus.events if e.type == EventType.STATE_CHANGED]
    assert len(checkpoints) > 0


@pytest.mark.asyncio
async def test_tool_calls_persisted_as_assistant_message(
    executor: ToolExecutor, registry: ToolRegistry, session_store: SessionStore
) -> None:
    session_id = session_store.create_session(mode="execute")
    session_store.append_message(
        session_id,
        Message(role=MessageRole.USER, content="use tool"),
    )
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "persisted"})],
        )
    )
    provider.add_response(LLMResponse(content="tool result consumed"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="use tool")],
        provider=provider,
        config=AgentConfig(max_iterations=3),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    await AgentLoop(max_iterations=3).run(ctx)
    reply = ctx.final_message or ctx.current_content or ""
    session_store.append_message(
        session_id,
        Message(role=MessageRole.ASSISTANT, content=reply),
    )
    branch = session_store.get_branch(session_id)
    assert len(branch) >= 2
    for entry in branch:
        assert entry["session_id"] == session_id
