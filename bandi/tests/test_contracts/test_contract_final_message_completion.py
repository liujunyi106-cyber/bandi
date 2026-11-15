"""契约：正常完成路径必须把正文落进 final_message。

缺陷：`parsing` 的 COMPLETED 分支只设 `terminal_reason`，从不给 `final_message`
赋值。`agent_runner` 的 reply 取 `final_message or current_content`，非流式路径
`current_content` 为空 → 助手消息已落库、API 却回空字符串。

现场：L1 回归 14 条门禁里 4 条「终态 COMPLETED 但回复为空」，会话轨迹里明明有
145 字的正常回答。
"""

from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.states import on_parsing
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.llm.mock import MockProvider


def _ctx(response: LLMResponse) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="你好")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.current_response = response
    # on_parsing 的正文取自 current_content（流式缓冲），不是 response.content
    ctx.current_content = response.content
    return ctx


class TestCompletedCarriesBody:
    @pytest.mark.asyncio
    async def test_normal_completion_sets_final_message(self) -> None:
        """正常完成：正文必须进 final_message。"""
        ctx = _ctx(LLMResponse(content="老板，已记录在案。", tool_calls=[]))
        await on_parsing(ctx)
        assert ctx.terminal_reason is TerminalReason.COMPLETED
        assert ctx.final_message == "老板，已记录在案。"

    @pytest.mark.asyncio
    async def test_blank_reply_still_gets_placeholder(self) -> None:
        """空回复仍走 NO_REPLY + 占位文案，不被本次修复带偏。"""
        ctx = _ctx(LLMResponse(content="   ", tool_calls=[]))
        await on_parsing(ctx)
        assert ctx.terminal_reason is TerminalReason.NO_REPLY
        assert ctx.final_message


class TestReplyDerivation:
    @pytest.mark.asyncio
    async def test_api_reply_not_blank_on_completed(self) -> None:
        """端到端口径：COMPLETED 终态下 API 拿到的 reply 必须非空。

        这正是回归现场——会话里已有正常回答，`reply` 却取到空串。
        """
        ctx = _ctx(LLMResponse(content="已处理完毕。", tool_calls=[]))
        await on_parsing(ctx)
        reply = (ctx.final_message or ctx.current_content or "").strip()
        assert reply == "已处理完毕。"

    @pytest.mark.asyncio
    async def test_multiline_body_preserved(self) -> None:
        """多行正文不得被截断或改写。"""
        body = "第一行\n\n- 要点一\n- 要点二\n\n结论。"
        ctx = _ctx(LLMResponse(content=body, tool_calls=[]))
        await on_parsing(ctx)
        assert ctx.final_message == body
