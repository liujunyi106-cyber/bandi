# 功能：6 项 Harness 差距项验收测试
# 对齐 docs/Harness工程差距分析与改进.md 清单

from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, RecoveryAction, TerminalReason
from bandi.agent.recovery_manager import RecoveryManager
from bandi.agent.states import on_executing, on_parsing, on_thinking
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult


class _RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _MockToolExecutor:
    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        return [
            ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
            for tc in tool_calls
        ]


def _make_ctx(messages: list[Message] | None = None) -> ExecutionContext:
    return ExecutionContext(
        messages=messages or [],
        provider=None,
        config=AgentConfig(),
        bus=_RecordingBus(),
    )


@pytest.mark.asyncio
async def test_p01_token_budget_exhausted_before_call() -> None:
    """预算已达上限时，on_thinking 在调 LLM 前终止。"""
    ctx = _make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
    ctx.config.token_budget_limit = 100
    ctx.consumed_tokens = 100
    await on_thinking(ctx)
    assert ctx.terminal_reason == TerminalReason.TOKEN_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_p02_refusal_triggers_safety_blocked() -> None:
    ctx = _make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
    ctx.current_response = LLMResponse(content="", refusal=True)
    await on_parsing(ctx)
    assert ctx.terminal_reason == TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_p02_content_filter_triggers_safety_blocked() -> None:
    ctx = _make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
    ctx.current_response = LLMResponse(content="", finish_reason="content_filter")
    await on_parsing(ctx)
    assert ctx.terminal_reason == TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_p03_escalate_after_10_consecutive_recoverable() -> None:
    mgr = RecoveryManager(escalate_threshold=10)
    action = RecoveryAction.RETRY
    for _ in range(10):
        action = await mgr.handle(TimeoutError("timeout"), "thinking")
    assert action == RecoveryAction.ESCALATE


@pytest.mark.asyncio
async def test_p03_consecutive_reset_on_turn_completed() -> None:
    mgr = RecoveryManager(escalate_threshold=10)
    for _ in range(9):
        await mgr.handle(TimeoutError("timeout"), "thinking")
    mgr.on_turn_completed()
    action = await mgr.handle(TimeoutError("timeout"), "thinking")
    assert action == RecoveryAction.RETRY


@pytest.mark.asyncio
async def test_p04_loop_detected_on_repeated_calls() -> None:
    ctx = _make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
    ctx.tool_executor = _MockToolExecutor()
    ctx.loop_detection_window = 3
    tc = [ToolCall(id="c1", name="bash", arguments={"command": "ls"})]
    for _ in range(3):
        ctx.current_tool_calls = tc
        await on_executing(ctx)
        if ctx.terminal_reason == TerminalReason.LOOP_DETECTED:
            break
    assert ctx.terminal_reason == TerminalReason.LOOP_DETECTED


# 完整退避测试见 tests/test_llm/test_circuit_breaker.py


def test_p05_circuit_breaker_and_retry_policy_exist() -> None:
    from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy

    policy = RetryPolicy()
    cb = CircuitBreaker()
    assert policy is not None
    assert cb is not None


# 完整截断测试见 tests/test_tools/test_tools.py


def test_p06_tool_executor_has_truncation_and_persist() -> None:
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    executor = ToolExecutor(registry=ToolRegistry())
    assert hasattr(executor, "_max_output_chars")
    assert hasattr(executor, "_truncate_output")
    assert hasattr(executor, "_persist_output")
    assert executor._max_output_chars > 0
