"""契约：不再「每轮都写记忆与草稿」。

缺陷实证：员工私聊里 `你好` / `ping` 这类回合也写三通道
（episodic + working + long_term），记忆注记显示「计入记忆 x3」，实际记的是
`用户: ping → 回答: 老板，pong。`。本契约钉住：**无跨轮价值**的回合不写经历、
不写草稿；有工具结果 / 显式要求记住 / 有结论信号的回合照写。"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.core.agent_runner import _persist_memory, _turn_has_reusable_value
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, ToolResult


class _IdleProvider:
    model_name = "idle"

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="")


def _ctx(raw_input: str, *, tools: list[Any] | None = None, reply: str = "") -> ExecutionContext:
    from unittest.mock import MagicMock

    ctx = ExecutionContext(
        messages=[], provider=_IdleProvider(), config=AgentConfig(), bus=MagicMock()
    )
    ctx.raw_input = raw_input
    ctx.turn_index = 1
    ctx.tool_results = tools or []
    ctx.final_message = reply
    return ctx


@pytest.mark.parametrize(
    ("raw_input", "reply"),
    [
        ("你好", "老板好！有什么可以帮你的？"),
        ("ping", "老板，pong。收到消息，一切正常。"),
        ("谢谢", "不客气。"),
        ("今天几号", "2026-11-06。"),
    ],
)
def test_chitchat_turn_has_no_reusable_value(raw_input: str, reply: str) -> None:
    assert not _turn_has_reusable_value(_ctx(raw_input, reply=reply), raw_input, reply)


def test_tool_work_has_reusable_value() -> None:
    ctx = _ctx(
        "帮我把报告导出成 CSV",
        tools=[ToolResult("tc-1", "write", True, "ok")],
        reply="导出完成",
    )
    assert _turn_has_reusable_value(ctx, ctx.raw_input, ctx.final_message)


def test_explicit_remember_has_reusable_value() -> None:
    ctx = _ctx("记住：部署端口是 7421", reply="好的")
    assert _turn_has_reusable_value(ctx, ctx.raw_input, ctx.final_message)


def test_conclusion_signal_has_reusable_value() -> None:
    ctx = _ctx("为什么导出报错", reply="原因是时区偏移，结论见下")
    assert _turn_has_reusable_value(ctx, ctx.raw_input, ctx.final_message)


@pytest.mark.asyncio
async def test_chitchat_turn_writes_nothing() -> None:
    """端到端：寒暄回合不写经历、不写草稿、不写长期记忆。"""
    from unittest.mock import MagicMock

    ctx = _ctx("你好", reply="老板好！有什么可以帮你的？")
    manager = MagicMock()
    manager.long_term.check_conflict.return_value = False

    await _persist_memory(ctx, manager, "sess-chitchat")

    manager.episodic.record.assert_not_called()
    manager.working.set.assert_not_called()
    manager.long_term.store.assert_not_called()


@pytest.mark.asyncio
async def test_tool_turn_still_writes_episode_and_scratch() -> None:
    """破坏者视角：真正做了事的回合必须照写，不能一刀切全禁。"""
    from unittest.mock import MagicMock

    ctx = _ctx(
        "帮我把报告导出成 CSV",
        tools=[ToolResult("tc-1", "write", True, "ok")],
        reply="导出完成，结论是 3 张表",
    )
    manager = MagicMock()
    manager.long_term.check_conflict.return_value = False

    await _persist_memory(ctx, manager, "sess-tool")

    manager.episodic.record.assert_called_once()
    manager.working.set.assert_called_once()
