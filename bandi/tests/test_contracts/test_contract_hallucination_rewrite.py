# 功能：契约——幻觉拦截前的校对重写轮（用户诉求 ：拦截不直接拒绝输出
# 先让模型基于证据校对重写；上限即拦截；工具调用轮不误拦；新请求重置计数）。
from __future__ import annotations

import types
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from tests.integration.helpers import MockProvider, MockToolExecutor, RecordingBus


class _ScriptedHC:
    # 功能：脚本化幻觉 checker——按序返回 blocked；记录调用次数（校对不可绕过）
    def __init__(self, blocked_seq: list[bool], spans: list[str] | None = None) -> None:
        self.calls = 0
        self._seq = blocked_seq
        self._spans = spans or ["项目完成于 1999 年（编造）"]

    async def check(self, text: str, context: str = "") -> Any:
        blocked = self._seq[min(self.calls, len(self._seq) - 1)]
        self.calls += 1
        return types.SimpleNamespace(
            l1=None,
            l2=types.SimpleNamespace(hallucination_spans=list(self._spans)),
            overall_score=1.0 if blocked else 0.0,
            blocked=blocked,
        )


def _ctx(provider: MockProvider, hc: Any) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="报告数据")],
        provider=provider,
        config=AgentConfig(max_iterations=5),
        bus=RecordingBus(),
        meta={"session_id": "sess-hallu-rewrite"},
    )
    ctx.raw_input = "报告数据"
    ctx.hallucination_checker = hc
    ctx.active_tools = []
    return ctx


@pytest.mark.asyncio
async def test_first_block_requests_rewrite_then_passes() -> None:
    """契约（核心）：首次 blocked 不终态——注入含待核对片段的校对指令并回
    thinking；重写通过后正常完成（红：现状直接 SAFETY_BLOCKED）。"""
    provider = MockProvider()
    provider.add_response(LLMResponse("编造：该项目完成于 1999 年，代号 X"))
    provider.add_response(LLMResponse("证据中无法确认完成时间，该数据未查证"))
    hc = _ScriptedHC([True, False])
    ctx = _ctx(provider, hc)

    reason = await AgentLoop(max_iterations=5).run(ctx)

    assert reason != TerminalReason.SAFETY_BLOCKED, "首次 blocked 不应直接拦截"
    assert hc.calls >= 2, "重写内容必须再次过 judge（校对不可绕过）"
    rewrite_msgs = [
        m
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM and "[事实校对]" in (m.content or "")
    ]
    assert rewrite_msgs, "必须注入含 [事实校对] 的校对指令"
    assert "1999" in rewrite_msgs[0].content or "项目完成于" in rewrite_msgs[0].content, (
        "校对指令须含 judge 指出的待核对片段"
    )
    assert int(ctx.meta.get("_hallucination_rewrite_count", 0)) == 1


@pytest.mark.asyncio
async def test_rewrite_exhausted_then_block() -> None:
    """契约：重写后仍 blocked 且达上限→ 终态拦截；不无限重写。
    轮次：原始 1 + 重写 2 = 3 次 LLM 调用 / 3 次 judge。"""
    provider = MockProvider()
    for text in (
        "编造说法：该项目完成于 1999 年 3 月，预算 123 万元整。",
        "编造说法二：项目由 X 团队验收通过，工期 88 天整。",
        "编造说法三：项目已交付海外市场，累计收益 456 万元。",
        "编造说法四：项目编号 ABC-999，负责人为张某。",
        "编造说法五：项目备案号 ZX-2025-888，验收日期为 6 月。",
    ):
        provider.add_response(LLMResponse(text))
    hc = _ScriptedHC([True])  # 永远 blocked
    ctx = _ctx(provider, hc)

    reason = await AgentLoop(max_iterations=8).run(ctx)

    assert reason == TerminalReason.SAFETY_BLOCKED
    assert "幻觉" in (ctx.final_message or "")
    assert hc.calls == 3, f"上限 2 轮重写 → judge 恰 3 次（实际 {hc.calls}）"
    assert provider.call_count == 3, f"不得无限重写（LLM 调用实际 {provider.call_count}）"


@pytest.mark.asyncio
async def test_tool_call_round_not_blocked() -> None:
    """契约（破坏者）：带 tool_calls 的轮不因幻觉检查被拦（工具调用非结论性
    陈述；结论轮才把关）——且工具轮不得调用 judge。"""
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="我先调用工具核实",
            tool_calls=[ToolCall(id="t1", name="probe", arguments={})],
        )
    )
    provider.add_response(LLMResponse("核实完成：该项目 1999 年 3 月交付，预算 123 万元整"))
    provider.add_response(LLMResponse("证据中无法确认交付时间与预算，该数据未查证，不建议采信"))
    hc = _ScriptedHC([True, False])  # 工具轮若被送检会直接拦截（红信号）
    ctx = _ctx(provider, hc)
    ctx.tool_executor = MockToolExecutor()

    reason = await AgentLoop(max_iterations=5).run(ctx)

    assert reason == TerminalReason.COMPLETED, f"工具轮不得被拦（实际 {reason}）"
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert tool_msgs, "工具必须真实执行（不得因幻觉检查被拦）"
    assert hc.calls == 2, f"工具轮必须跳过 judge（实际 {hc.calls} 次：结论轮 1 + 重写轮 1）"


@pytest.mark.asyncio
async def test_on_init_resets_rewrite_counter() -> None:
    """契约：新请求边界清计数（与上轮终态残留同族维护）。"""
    from bandi.agent.states import on_init

    provider = MockProvider()
    ctx = _ctx(provider, _ScriptedHC([False]))
    ctx.meta["_hallucination_rewrite_count"] = 1
    ctx.current_state = "init"

    await on_init(ctx)

    assert int(ctx.meta.get("_hallucination_rewrite_count", 0)) == 0, (
        "on_init 必须清 _hallucination_rewrite_count"
    )
