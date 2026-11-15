# 功能：契约——子代理与父会话共享事件总线时，流式/工具事件必须按会话隔离
# （不得串台到父会话 UI）。
from __future__ import annotations

from typing import Any

import pytest

from bandi.core.agent_runner import _HandlerBindings
from bandi.core.events import Event, EventType


class _RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def on_content_delta(self, session_id: str, content: str) -> None:
        self.calls.append(("delta", session_id, content))

    async def on_tool_call_start(self, session_id: str, tool_name: str, _x: Any) -> None:
        self.calls.append(("tool_start", session_id, tool_name))

    async def on_tool_result(self, *args: Any) -> None:
        self.calls.append(("tool_result", *args))

    async def on_tool_progress(self, *args: Any) -> None:
        self.calls.append(("tool_progress", *args))


@pytest.mark.asyncio
async def test_contract_stream_event_from_other_session_not_forwarded() -> None:
    """契约（会话隔离·破坏者）：子代理（子会话）的 content_delta 不得串台到
    父会话 handler——修复前回调不比对 session_id（子代理逐字输出混入父 UI）。"""
    handler = _RecordingHandler()
    bindings = _HandlerBindings(handler, "sess-parent")
    event = Event(
        type=EventType.LLM_STREAM_EVENT,
        data={"type": "content_delta", "content": "子代理内部输出"},
        session_id="sess-child",
    )
    await bindings._on_stream_event(event)
    assert handler.calls == [], f"跨会话流式事件不得转发，实际 {handler.calls}"


@pytest.mark.asyncio
async def test_contract_tool_event_from_other_session_not_forwarded() -> None:
    """契约（工具事件隔离）：data.session_id 形态（executor._emit_event）同样按
    会话过滤。"""
    handler = _RecordingHandler()
    bindings = _HandlerBindings(handler, "sess-parent")
    event = Event(
        type=EventType.TOOL_INVOKED,
        data={"session_id": "sess-child", "tool_name": "bash"},
    )
    await bindings._on_tool_invoked(event)
    assert handler.calls == []


@pytest.mark.asyncio
async def test_contract_own_session_event_forwarded() -> None:
    """契约（回归）：本会话事件正常转发（修复不得误杀同会话事件）。"""
    handler = _RecordingHandler()
    bindings = _HandlerBindings(handler, "sess-parent")
    event = Event(
        type=EventType.LLM_STREAM_EVENT,
        data={"type": "content_delta", "content": "hi"},
        session_id="sess-parent",
    )
    await bindings._on_stream_event(event)
    assert handler.calls == [("delta", "sess-parent", "hi")]
