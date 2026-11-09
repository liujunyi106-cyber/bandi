# 功能：请求前折叠视图（fold-view）契约
# ②最近活跃轮（含刚回填的工具结果）不折叠；
# ③折叠只针对更早轮的超长 TOOL 输出，占位符保留 role/tool_call_id（API 配对合法）；
# ④阈值<=0 关闭；⑤幂等（对已折叠视图再次折叠不产生新占位符叠加）。

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import Message, MessageRole, ToolCall

_PLACEHOLDER = "[旧工具输出已折叠，完整内容保留在会话历史中]"


def _big_tool(turn: int, chars: int, name: str = "read", cid: str = "") -> Message:
    return Message(
        role=MessageRole.TOOL,
        content="字" * chars,
        tool_call_id=cid or f"c_{name}_{turn}",
        name=name,
        turn_index=turn,
    )


def _assistant_tool(turn: int, name: str = "read") -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=f"c_{name}_{turn}", name=name, arguments={"path": "/x"})],
        turn_index=turn,
    )


def _history() -> list[Message]:
    """多轮对话：旧轮超大工具输出（turn1）+ 前一轮已完成段的工具输出（turn3）
    + 当前 thinking 轮刚回填的大工具结果（turn5=max，on_thinking 回填语义）。"""
    return [
        Message(role=MessageRole.SYSTEM, content="sys", turn_index=0),
        Message(role=MessageRole.USER, content="第一问", turn_index=1),
        _assistant_tool(1),
        _big_tool(1, 20000, cid="c_read_1"),
        Message(role=MessageRole.ASSISTANT, content="第一轮结论", turn_index=2),
        Message(role=MessageRole.USER, content="第二问", turn_index=3),
        _assistant_tool(3, name="bash"),
        _big_tool(3, 5000, name="bash", cid="c_bash_3"),
        Message(role=MessageRole.ASSISTANT, content="第二轮进行中结论", turn_index=4),
        Message(role=MessageRole.USER, content="继续", turn_index=5),
        _assistant_tool(5, name="read"),
        _big_tool(5, 6000, name="read", cid="c_read_5"),
    ]


@pytest.fixture
def fold():
    from bandi.agent import request_view

    return request_view.build_request_view


def test_old_rounds_folded_recent_two_kept(fold) -> None:
    """契约（默认 keep=2，与剪枝保护轮同族）：最近两轮工具原文（turn3/turn5）保留
    更早轮（turn1）折叠为占位符——长工具链「t 轮结果被 t+2 引用」不失源。"""
    from bandi.agent.request_view import build_request_view

    view = build_request_view(_history(), fold_max_tool_chars=500)
    tool_msgs = [m for m in view if m.role == MessageRole.TOOL]
    old = next(m for m in tool_msgs if m.tool_call_id == "c_read_1")
    assert old.content.startswith(_PLACEHOLDER), old.content
    assert "重新调用该工具" in old.content and "read" in old.content, "须告知如何取回完整内容"
    assert old.tool_call_id == "c_read_1", "占位符必须保留 tool_call_id（防 API 孤儿）"
    # turn3（max-2）与 turn5（当前刚回填）保留原文
    bash = next(m for m in tool_msgs if m.tool_call_id == "c_bash_3")
    assert bash.content == "字" * 5000, "最近两轮工具原文必须保留（keep=2，T-029）"
    current = next(m for m in tool_msgs if m.tool_call_id == "c_read_5")
    assert current.content == "字" * 6000, "当前轮刚回填工具结果必须保留原文"


def test_keep_one_folds_more_aggressively(fold) -> None:
    """契约：显式 keep_recent_turns=1 时只保留最近一轮原文（参数可控）。"""
    from bandi.agent.request_view import build_request_view

    view = build_request_view(_history(), fold_max_tool_chars=500, keep_recent_turns=1)
    tool_msgs = {m.tool_call_id: m for m in view if m.role == MessageRole.TOOL}
    assert tool_msgs["c_read_1"].content.startswith(_PLACEHOLDER)
    assert tool_msgs["c_bash_3"].content.startswith(_PLACEHOLDER), "keep=1 时 max-2 轮也应折叠"
    assert tool_msgs["c_read_5"].content == "字" * 6000


def test_original_messages_untouched(fold) -> None:
    """契约：折叠只产生新视图，原 ctx.messages 的对象与内容全部原样。"""
    from bandi.agent.request_view import build_request_view

    history = _history()
    originals = [(id(m), m.content, m.role) for m in history]
    build_request_view(history, fold_max_tool_chars=500)
    after = [(id(m), m.content, m.role) for m in history]
    assert originals == after, "fold 不得改写 ctx.messages（可逆性）"


def test_threshold_zero_disables(fold) -> None:
    """契约：fold_max_tool_chars<=0 时返回视图与输入内容一致（零折叠）。"""
    from bandi.agent.request_view import build_request_view

    history = _history()
    view = build_request_view(history, fold_max_tool_chars=0)
    assert [m.content for m in view if m.role == MessageRole.TOOL] == [
        m.content for m in history if m.role == MessageRole.TOOL
    ]


def test_short_outputs_never_folded(fold) -> None:
    """契约：低于阈值的旧轮工具输出保持原文（小结果不折叠）。"""
    from bandi.agent.request_view import build_request_view

    msgs = [
        Message(role=MessageRole.SYSTEM, content="sys", turn_index=0),
        Message(role=MessageRole.USER, content="q1", turn_index=1),
        _assistant_tool(1),
        _big_tool(1, 100, cid="c_small"),
        Message(role=MessageRole.USER, content="q2", turn_index=2),
    ]
    view = build_request_view(msgs, fold_max_tool_chars=500)
    small = next(m for m in view if m.role == MessageRole.TOOL)
    assert small.content == "字" * 100


def test_idempotent(fold) -> None:
    """契约：对折叠视图再次折叠不产生占位符叠加（占位符短于阈值天然幂等）。"""
    from bandi.agent.request_view import build_request_view

    view1 = build_request_view(_history(), fold_max_tool_chars=500)
    view2 = build_request_view(view1, fold_max_tool_chars=500)
    assert [m.content for m in view1 if m.role == MessageRole.TOOL] == [
        m.content for m in view2 if m.role == MessageRole.TOOL
    ]


def test_protocol_pairs_survive_fold(fold) -> None:
    """契约：折叠后 assistant(tool_calls)↔tool 结果配对完整（可直接过 guard_messages）。"""
    from bandi.agent.request_view import build_request_view
    from bandi.agent.tool_message_guard import guard_messages

    view = build_request_view(_history(), fold_max_tool_chars=500)
    guarded = guard_messages(view)
    assert len(guarded) == len(view), "折叠不得破坏工具消息配对"


def test_fold_reduces_token_estimate(fold) -> None:
    """契约：折叠视图字符数显著低于原始视图（省 token 目标）。"""
    from bandi.agent.request_view import build_request_view

    history = _history()
    view = build_request_view(history, fold_max_tool_chars=500)
    before = sum(len(m.content or "") for m in history)
    after = sum(len(m.content or "") for m in view)
    assert after < before * 0.5, "折叠必须带来显著体积缩减"


@pytest.mark.asyncio
async def test_fold_in_production_request_path(monkeypatch) -> None:
    """契约（生产主链路）：第二轮 thinking 发给 LLM 的旧轮大工具输出为占位符
    而 ctx.messages 原文保留；刚回填的当轮工具结果不折叠。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.states import on_thinking
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, TokenUsage, ToolResult
    from bandi.llm.base import BaseProvider

    class _Bus(EventBus):
        def __init__(self) -> None:
            super().__init__()
            self.events = []

        async def emit(self, event) -> None:
            self.events.append(event)

    class _Rec(BaseProvider):
        def __init__(self) -> None:
            super().__init__(None)
            self.seen: list[list[dict[str, Any]]] = []

        @property
        def model_name(self) -> str:
            return "stub"

        @property
        def protocol(self) -> str:
            return "openai"

        @property
        def supports_streaming(self) -> bool:
            return False

        async def complete(self, messages, tools=None, **kwargs):
            self.seen.append(messages)
            return LLMResponse(content="ok", usage=TokenUsage(prompt_tokens=3, completion_tokens=1))

        async def stream(self, messages, tools=None, **kwargs):
            yield

    provider = _Rec()
    ctx = ExecutionContext(
        messages=_history(),
        provider=provider,
        config=AgentConfig(),
        bus=_Bus(),
    )
    ctx.config.fold_tool_view_chars = 500
    ctx.turn_index = 5  # 下一 thinking 轮 = 6（历史 max=5），刚回填结果落在最新轮
    ctx.raw_input = "第二问"
    ctx.current_tool_calls = [ToolCall(id="c_bash_cur", name="bash", arguments={"cmd": "echo hi"})]
    ctx.tool_results = [
        ToolResult(
            tool_call_id="c_bash_cur",
            tool_name="bash",
            success=True,
            output="当轮结果" * 3000,
        )
    ]
    await on_thinking(ctx)

    assert provider.seen, "必须发起 LLM 调用"
    sent = provider.seen[-1]
    tool_contents = [str(m.get("content") or "") for m in sent if m.get("role") == "tool"]
    assert any(_PLACEHOLDER in c for c in tool_contents), "旧轮大工具输出必须在请求视图折叠"
    assert any("当轮结果" in c for c in tool_contents), "当轮刚回填的工具结果不得折叠"
    originals = [m.content for m in ctx.messages if m.role == MessageRole.TOOL]
    assert any(len(c) > 1000 for c in originals), "ctx.messages 原文必须保留（可逆性）"
