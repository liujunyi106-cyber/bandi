# 功能：状态转移原因契约（Transition.label / StateMachine.last_reason / STATE_CHANGED 事件 reason）
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.base import ExecutionContext, StateMachine, Transition
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.llm.mock import MockProvider


class _Bus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _make_ctx(raw_input: str) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello")],
        provider=MockProvider(),  # type: ignore[arg-type]
        config=AgentConfig(),
        bus=_Bus(),
    )
    ctx.raw_input = raw_input
    return ctx


def test_state_machine_tracks_transition_reason() -> None:
    """条件命中时 last_reason 记录该转移 label；默认分支记录其 label。"""
    sm = StateMachine()
    sm.register(
        "init",
        [
            Transition(
                target="thinking",
                condition=lambda ctx: bool(ctx.raw_input),
                label="input_present",
            ),
            Transition(target="terminated", label="no_input"),
        ],
    )
    ctx = _make_ctx("go")
    assert sm.next(ctx) == "thinking"
    assert sm.last_reason == "input_present"
    ctx = _make_ctx("")
    assert sm.next(ctx) == "terminated"
    assert sm.last_reason == "no_input"


def test_state_machine_reason_defaults() -> None:
    """无命中落 terminated 记 no_condition_matched；terminated 粘滞记 terminal_sticky。"""
    sm = StateMachine()
    sm.register("init", [Transition(target="thinking", condition=lambda ctx: False, label="never")])
    ctx = _make_ctx("x")
    assert sm.next(ctx) == "terminated"
    assert sm.last_reason == "no_condition_matched"
    ctx.current_state = "terminated"
    assert sm.next(ctx) == "terminated"
    assert sm.last_reason == "terminal_sticky"


@pytest.mark.asyncio
async def test_state_changed_event_carries_reason() -> None:
    """生产主链路：每个 STATE_CHANGED（含 to）必须携非空 reason（默认转移表全标注）。"""
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hi"))
    bus = _Bus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    await loop.run(ctx)
    state_events = [
        e for e in bus.events if e.type == EventType.STATE_CHANGED and "to" in (e.data or {})
    ]
    assert state_events, "必须发出 STATE_CHANGED"
    assert all("reason" in ev.data for ev in state_events), "每个 STATE_CHANGED 必须带 reason 字段"
    assert any(ev.data.get("reason") for ev in state_events), "reason 必须非空（默认转移表全标注）"
