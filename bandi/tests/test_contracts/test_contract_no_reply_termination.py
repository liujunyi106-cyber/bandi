"""
   写方：states.on_parsing——终答判定（无 tool_calls 时的终止语义）。
   读方：transitions（terminal_reason 非 None → terminated）、agent_runner 终态集合。

行为族（钉承诺行为不变式，不钉实现）：
   无 tool_calls + reply 非空 → COMPLETED（回归锚，正常纯文本回答）
   无 tool_calls + reply 为空 "" → NO_REPLY（显式非完成终态）+
       final_message 非空说明 + parse_outcome 仍置 NONE
   无 tool_calls + reply 纯空白 → NO_REPLY（不得当"完成"）
   无 tool_calls + reply 全为零宽空格 → NO_REPLY（破坏者：\u200b 视觉空白绕过）
   有 tool_calls 完整 → PARSED（不因空 reply 改动进入终止分支——空回复
       契约只作用于无工具分支）
   有 tool_calls 且 length 截断 → PARTIAL（同上，进 executing 不动）
   有 tool_calls 时 reply 为空 → 不进 NO_REPLY（空 reply 判定仅限无 tool_calls）

破坏者断言：
   - 零宽空格（\u200b）组成的回复不得被视为有效纯文本回答（不得 COMPLETED）
   - 极短但非空的回复（"." / "👍"）不受影响，仍 COMPLETED（不误伤短确认回复）
   - NO_REPLY 不得携带 COMPLETED 双重标记
"""

from typing import Any

import pytest


def _parsing_ctx(
    tool_calls: list[Any] | None,
    content: str = "",
    finish_reason: str | None = None,
) -> Any:
    """构造最小可跑 on_parsing 的 ExecutionContext（沿用 parse_outcome 契约模板）。"""
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
        content=content, tool_calls=tool_calls or [], finish_reason=finish_reason, refusal=False
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


# ── 写方：on_parsing 空回复终止语义 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_text_reply_stays_completed() -> None:
    """无工具 + 非空文本 → COMPLETED（回归锚——正常纯文本回答不受扰动）。"""
    from bandi.agent.base import ParseOutcome, TerminalReason
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([], content="最终回答")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.NONE
    assert ctx.terminal_reason is TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_empty_reply_is_no_reply_not_completed() -> None:
    """无工具 + 空回复 → NO_REPLY 显式非完成终态（不得放行 COMPLETED）。"""
    from bandi.agent.base import ParseOutcome, TerminalReason
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([], content="")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.NONE
    assert ctx.terminal_reason is TerminalReason.NO_REPLY, (
        f"空回复必须判 NO_REPLY（实际 {ctx.terminal_reason}）——空产物放行 COMPLETED 即旧缺陷回归"
    )
    assert (ctx.final_message or "").strip(), "NO_REPLY 必须携带可读的 final_message"
    assert ctx.terminal_reason is not TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_whitespace_reply_is_no_reply() -> None:
    """无工具 + 纯空白回复 → NO_REPLY（空格/换行/制表不得视为有效回答）。"""
    from bandi.agent.base import TerminalReason
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([], content="  \n\t  ")
    await on_parsing(ctx)
    assert ctx.terminal_reason is TerminalReason.NO_REPLY


@pytest.mark.asyncio
async def test_zero_width_reply_is_no_reply() -> None:
    """（破坏者）：全零宽空格回复 → NO_REPLY——\u200b 视觉空白不得绕过空回复判定。"""
    from bandi.agent.base import TerminalReason
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([], content="\u200b\u200b \u200b")
    await on_parsing(ctx)
    assert ctx.terminal_reason is TerminalReason.NO_REPLY


@pytest.mark.asyncio
async def test_short_nonblank_reply_not_harmed() -> None:
    """破坏者反例：极短但非空的回复（"." / emoji）不得被误伤为 NO_REPLY。"""
    from bandi.agent.base import TerminalReason
    from bandi.agent.states import on_parsing

    for short in (".", "👍"):
        ctx = _parsing_ctx([], content=short)
        await on_parsing(ctx)
        assert ctx.terminal_reason is TerminalReason.COMPLETED, (
            f"短确认回复 {short!r} 必须保持 COMPLETED（实际 {ctx.terminal_reason}）"
        )


@pytest.mark.asyncio
async def test_tool_calls_with_empty_reply_not_no_reply() -> None:
    """
    有 tool_calls 时 reply 为空是合法形态（模型走工具轮可无旁白）——
    空回复契约只作用于无工具分支，不得误伤工具轮。"""
    from bandi.agent.base import ParseOutcome
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([_tc()], content="", finish_reason="tool_calls")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.PARSED


@pytest.mark.asyncio
async def test_truncated_tool_calls_still_partial() -> None:
    """有 tool_calls 且 length 截断 → PARTIAL（空旁白不改变截断路径）。"""
    from bandi.agent.base import ParseOutcome
    from bandi.agent.states import on_parsing

    ctx = _parsing_ctx([_tc()], content="", finish_reason="length")
    await on_parsing(ctx)
    assert ctx.parse_outcome is ParseOutcome.PARTIAL


# ── 读方：transitions 转发一致性（NO_REPLY 属非完成终态族 → terminated） ─────


def test_no_reply_routes_to_terminated_like_other_terminal() -> None:
    """
    族级：NO_REPLY 作为 terminal_reason 必须让状态机直达 terminated
    （同 SAFETY_BLOCKED 短路路径——不得被 verifier 门绕道 reflecting 造成空转）。"""
    from bandi.agent.base import ExecutionContext, TerminalReason
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
    ctx.current_state = "parsing"
    ctx.terminal_reason = TerminalReason.NO_REPLY
    ctx.parse_outcome = None
    ctx.verifier = object()
    sm = build_default_machine()
    assert sm.next(ctx) == "terminated", (
        f"NO_REPLY 必须直达 terminated（实际 {sm.next(ctx)}）——滞留 parsing 即空转回归"
    )
