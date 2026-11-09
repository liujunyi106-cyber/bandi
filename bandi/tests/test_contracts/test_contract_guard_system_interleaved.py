# 功能：guard SYSTEM 插队契约——系统注入消息不得使真实 tool 结果被判孤儿
# assistant/tool 消息即重置 pending → 后续真实 tool 结果整批被删（不可逆）。
# 修复语义：仅「会话角色」消息（user/纯文本 assistant）重置 pending；SYSTEM
# 注入视为同回合内插队，不重置——真实结果保留，真孤儿照删。
from __future__ import annotations

import pytest

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.compression_pipeline import COMPACT_BOUNDARY_PREFIX


def _tc(cid: str, name: str = "bash") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments={"x": 1})


def _assistant_with_calls(*calls: ToolCall) -> Message:
    return Message(role=MessageRole.ASSISTANT, content="执行", tool_calls=list(calls))


def _tool_msg(cid: str) -> Message:
    return Message(role=MessageRole.TOOL, content="ok", tool_call_id=cid)


class TestSystemInterleaved:
    def test_compact_boundary_between_call_and_result_keeps_result(self) -> None:
        """压缩边界（SYSTEM）插在 assistant 与 tool 之间——真实结果必须保留。"""
        from bandi.agent.tool_message_guard import drop_orphan_tool_messages

        msgs = [
            _assistant_with_calls(_tc("t1")),
            Message(
                role=MessageRole.SYSTEM,
                content=f"{COMPACT_BOUNDARY_PREFIX} stage=progressive msgs=20->8",
            ),
            _tool_msg("t1"),
        ]
        kept = drop_orphan_tool_messages(msgs)
        assert [m.tool_call_id for m in kept if m.role == MessageRole.TOOL] == ["t1"], (
            "SYSTEM 注入插队不得使真实 tool 结果被判孤儿（旧实现整批删除）"
        )

    def test_summary_system_between_call_and_result_keeps_result(self) -> None:
        """摘要/提示类 SYSTEM 消息同理——不重置 pending。"""
        from bandi.agent.tool_message_guard import drop_orphan_tool_messages

        msgs = [
            _assistant_with_calls(_tc("t1"), _tc("t2", "read")),
            Message(role=MessageRole.SYSTEM, content="[上下文摘要] 进度……"),
            _tool_msg("t1"),
            Message(role=MessageRole.SYSTEM, content="[置信度提示] 请确认"),
            _tool_msg("t2"),
        ]
        kept = drop_orphan_tool_messages(msgs)
        ids = [m.tool_call_id for m in kept if m.role == MessageRole.TOOL]
        assert ids == ["t1", "t2"], f"两个结果都必须保留（实际 {ids}）"

    def test_true_orphan_still_dropped_after_system(self) -> None:
        """破坏者：SYSTEM 不重置 ≠ 放行孤儿——从未被声明的 tool 仍剔除。"""
        from bandi.agent.tool_message_guard import drop_orphan_tool_messages

        msgs = [
            _assistant_with_calls(_tc("t1")),
            Message(role=MessageRole.SYSTEM, content="[反思反馈] 换个思路"),
            _tool_msg("ghost"),
        ]
        kept = drop_orphan_tool_messages(msgs)
        assert [m.tool_call_id for m in kept if m.role == MessageRole.TOOL] == []

    def test_user_message_still_resets_pending(self) -> None:
        """回归：会话角色消息（user）仍重置 pending（跨回合旧 id 复用视为孤儿）。"""
        from bandi.agent.tool_message_guard import drop_orphan_tool_messages

        msgs = [
            _assistant_with_calls(_tc("t1")),
            _tool_msg("t1"),
            Message(role=MessageRole.USER, content="新问题"),
            _tool_msg("t1"),
        ]
        kept = drop_orphan_tool_messages(msgs)
        ids = [m.tool_call_id for m in kept if m.role == MessageRole.TOOL]
        assert ids == ["t1"], "user 重置后复用旧 id 的 TOOL 仍为孤儿（既有契约不变）"


@pytest.mark.parametrize("system_content", ["[压缩边界] x", "[上下文摘要] y", "[置信度提示] z"])
def test_guard_messages_end_to_end_keeps_system_paired(system_content: str) -> None:
    """生产入口 guard_messages：SYSTEM 插队 + 缺结果补 stub 组合后配对完整。"""
    from bandi.agent.tool_message_guard import guard_messages

    msgs = [
        _assistant_with_calls(_tc("t1")),
        Message(role=MessageRole.SYSTEM, content=system_content),
    ]
    guarded = guard_messages(msgs)
    tool_ids = [m.tool_call_id for m in guarded if m.role == MessageRole.TOOL]
    assert tool_ids == ["t1"], "guard_messages 必须先补 stub 再剔孤儿（合成后配对完整）"
