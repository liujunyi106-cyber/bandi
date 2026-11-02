"""
   写方：states.on_parsing 唯一置值点（NONE/PARSED/PARTIAL 三值生产可达）。
   读方：transitions.parsing 分支消费（executing 判定 + should_terminate 提前短路）。
语义钉死：
   NONE = 无 tool_calls 纯文本回答（终态 COMPLETED 不变）
   PARSED = 有 tool_calls 且 finish_reason 非 length 截断 → executing（现状一致）
   PARTIAL = 有 tool_calls 但 finish_reason=="length"（截断，参数可能不完整）
             → 同走 executing（provider 已尽力解析，executor 校验参数）
             显式记录供观测与未来策略化（处置策略=开放决策，见交付文档）
   （PARSE_FAILED 死枚举已删除——零写方零读方）

破坏者断言：
   - finish_reason=="length" 的截断 tool_calls 不得被静默标为 PARSED（旧盲区）
   - 安全拦截后 parsing 直达 terminated，不得绕道 reflecting（时序契约）
   - 3 个布尔死方法必须保持删除（防复活：tools_parsed/parse_failed/partial_with_observation）
"""

from typing import Any

import pytest


def _parsing_ctx(
    tool_calls: list[Any] | None,
    content: str = "",
    finish_reason: str | None = None,
    refusal: bool = False,
) -> Any:
    """构造最小可跑 on_parsing 的 ExecutionContext。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, Message, MessageRole

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=_ProviderStub(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.current_response = LLMResponse(
        content=content, tool_calls=tool_calls or [], finish_reason=finish_reason, refusal=refusal
    )
    ctx.current_content = content or None
    ctx.current_tool_calls = tool_calls
    ctx.raw_input = "任务"
    return ctx


class _ProviderStub:  # 功能：满足 ExecutionContext 类型面的最小 provider 桩
    model_name = "mock"
    protocol = "openai"
    supports_streaming = False


def _tc(call_id: str = "c1") -> Any:
    from bandi.core.message import ToolCall

    return ToolCall(id=call_id, name="echo", arguments={"x": 1})


# ── 写方：on_parsing 置值 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parsing_full_tool_calls_sets_parsed() -> None:
    from bandi.agent.base import ParseOutcome
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([_tc()], finish_reason="tool_calls")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.PARSED, (
        f"完整 tool_calls 必须标 PARSED（实际 {ctx.parse_outcome}）"
    )


@pytest.mark.asyncio
async def test_parsing_truncated_length_sets_partial() -> None:
    from bandi.agent.base import ParseOutcome
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([_tc()], finish_reason="length")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.PARTIAL, (
        f"length 截断的 tool_calls 必须标 PARTIAL（实际 {ctx.parse_outcome}）——"
        f"静默 PARSED 即为旧盲区回归"
    )


@pytest.mark.asyncio
async def test_parsing_no_tool_calls_sets_none_and_completes() -> None:
    from bandi.agent.base import ParseOutcome, TerminalReason
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([], content="最终回答")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.NONE
    assert ctx.terminal_reason is TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_parse_outcome_defaults_unset_before_parsing() -> None:
    """
    「NONE 解析完（无工具）」是两个状态，不得混淆默认值。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="x")],
        provider=_ProviderStub(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    assert ctx.parse_outcome is None, "初始必须未置值（None≠NONE）"


# ── 读方：transitions 消费 ─────────────────────────────────────────────────


def _machine_ctx(state: str = "parsing", **attrs: Any) -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.agent.transitions import build_default_machine
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="x")],
        provider=_ProviderStub(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.current_state = state
    for k, v in attrs.items():
        setattr(ctx, k, v)
    sm = build_default_machine()
    return ctx, sm


def test_transition_parsed_goes_executing() -> None:
    from bandi.agent.base import ParseOutcome

    ctx, sm = _machine_ctx(parse_outcome=ParseOutcome.PARSED)
    assert sm.next(ctx) == "executing"


def test_transition_partial_still_goes_executing() -> None:
    """
    处置策略化为开放决策，不因 PARTIAL 卡死循环）。"""
    from bandi.agent.base import ParseOutcome

    ctx, sm = _machine_ctx(parse_outcome=ParseOutcome.PARTIAL)
    assert sm.next(ctx) == "executing"


def test_transition_safety_blocked_short_circuits_to_terminated() -> None:
    """
    terminated——原顺序下 needs_verification 会先命中而绕道 reflecting
    （on_reflecting 早退兜住但多绕一拍 STATE_CHANGED）。"""
    from bandi.agent.base import TerminalReason

    ctx, sm = _machine_ctx(
        terminal_reason=TerminalReason.SAFETY_BLOCKED,
        verifier=object(),  # 存在 verifier 才能复现绕道路径
        parse_outcome=None,
    )
    assert sm.next(ctx) == "terminated", (
        f"安全拦截必须直达 terminated（实际 {sm.next(ctx)}）——绕道 reflecting 即回归"
    )


@pytest.mark.asyncio
async def test_loop_end_to_end_tools_then_answer_then_final_none() -> None:
    """
    executing（STATE_CHANGED 出现），末轮纯文本 NONE 终止 COMPLETED。
    断言最终产物（状态轨迹 + 终态），不探测内部字段。"""
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType
    from bandi.core.message import LLMResponse, Message, MessageRole, ToolResult
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    provider.add_response(
        LLMResponse(content="", tool_calls=[_tc("call_1")], finish_reason="tool_calls")
    )
    provider.add_response(LLMResponse(content="完成回答", tool_calls=[]))

    class _Exec:
        async def execute(self, tool_calls: list[Any]) -> list[Any]:
            return [
                ToolResult(tool_call_id=t.id, tool_name=t.name, success=True, output="ok")
                for t in tool_calls
            ]

    visited: list[str] = []

    async def track(event: Event) -> None:
        if event.type == EventType.STATE_CHANGED:
            visited.append(event.data.get("to", ""))

    bus = EventBus()
    await bus.subscribe(EventType.STATE_CHANGED, track)
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="任务"),
        ],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.tool_executor = _Exec()

    reason = await AgentLoop(max_iterations=5).run(ctx)

    assert reason is TerminalReason.COMPLETED
    assert "executing" in visited, f"工具轮必须经 PARSED→executing（实际状态轨迹 {visited}）"
    assert visited[-1] == "terminated"


# ── 死方法删除钉（防复活）──────────────────────────────────────────────────


def test_dead_boolean_methods_removed() -> None:
    """
    三个恒值死方法必须保持删除——任何复活即为「双真相源」回归。"""
    from bandi.agent.base import ExecutionContext

    for dead in ("tools_parsed", "parse_failed", "partial_with_observation"):
        assert not hasattr(ExecutionContext, dead), f"{dead} 是死方法，必须保持删除"
