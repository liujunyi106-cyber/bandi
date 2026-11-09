# 功能：契约——工具失败容错（用户诉求：失败要尝试其他工具/总结
# 不能失败直接拒绝输出；硬终止前给一次「终止前总结」机会）。
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus


class _AlwaysFailTool(BaseTool):
    name: str = "boom"
    description: str = "总是失败"
    category: ToolCategory = ToolCategory.UTIL

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        raise RuntimeError("boom failure")


def _mk_executor() -> ToolExecutor:
    reg = ToolRegistry()
    reg.register_instance(_AlwaysFailTool())
    return ToolExecutor(registry=reg, execution_timeout=15)


def _loaded_executor() -> ToolExecutor:
    # 预置：同工具已连续失败达阈值 + 暂停反复调用计数达硬终止前一步
    ex = _mk_executor()
    ex._failure_count["boom"] = 3
    ex._block_count["boom"] = 2
    return ex


def _ctx(provider: MockProvider, executor: ToolExecutor) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="帮我跑一下任务")],
        provider=provider,
        config=AgentConfig(max_iterations=6),
        bus=RecordingBus(),
        meta={"session_id": "sess-fail-recover"},
    )
    ctx.raw_input = "帮我跑一下任务"
    ctx.tool_executor = executor
    ctx.active_tools = executor.registry.list_schemas()
    return ctx


@pytest.mark.asyncio
async def test_hard_abort_gives_one_summary_chance() -> None:
    """契约（核心）：硬终止阈值触发时不直接拒绝输出——注入「终止前总结」指令
    并回 thinking 再给模型一次输出机会；模型总结后正常完成（红：现状直接
    TOOL_ERROR 终态、无总结机会）。"""
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            "再试一次",
            tool_calls=[ToolCall(id="t1", name="boom", arguments={})],
        )
    )
    provider.add_response(LLMResponse("总结：任务未完成，boom 工具连续失败；建议人工处理。"))
    ctx = _ctx(provider, _loaded_executor())

    reason = await AgentLoop(max_iterations=6).run(ctx)

    assert reason != TerminalReason.TOOL_ERROR, "硬终止前必须给总结机会（不得直接拒绝输出）"
    assert reason == TerminalReason.COMPLETED
    summary_msgs = [
        m for m in ctx.messages if m.role == MessageRole.SYSTEM and "总结" in (m.content or "")
    ]
    assert summary_msgs, "必须注入「终止前总结」指令消息"
    assert ctx.meta.get("_abort_summary_used") is True


@pytest.mark.asyncio
async def test_second_hard_abort_terminates() -> None:
    """契约（防死循环兜底）：总结机会用过后仍硬撞暂停工具 → 终止（TOOL_ERROR）。
    同请求内两阶段：第一次硬撞给总结机会 → 模型仍执迷再调 → 第二次硬撞终止。"""
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            "再试一次",
            tool_calls=[ToolCall(id="t1", name="boom", arguments={})],
        )
    )
    provider.add_response(
        LLMResponse(
            "不听劝再试",
            tool_calls=[ToolCall(id="t2", name="boom", arguments={})],
        )
    )
    ctx = _ctx(provider, _loaded_executor())

    reason = await AgentLoop(max_iterations=6).run(ctx)

    assert reason == TerminalReason.TOOL_ERROR


@pytest.mark.asyncio
async def test_on_init_clears_abort_summary_flag() -> None:
    """契约：新请求边界清 `_abort_summary_used`（与同族边界清理一致）。"""
    from bandi.agent.states import on_init

    provider = MockProvider()
    ctx = _ctx(provider, _mk_executor())
    ctx.meta["_abort_summary_used"] = True
    ctx.current_state = "init"

    await on_init(ctx)

    assert ctx.meta.get("_abort_summary_used") is None
