# 功能：契约测试——thinking 模式带工具调用时 reasoning_content 必须回传（防上游 400）
from __future__ import annotations

from bandi.core.message import (
    Message,
    MessageRole,
    ToolCall,
    message_from_entry,
    message_to_dict,
)


class TestReasoningContentPassthrough:
    def test_assistant_with_reasoning_content_included(self) -> None:
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "a"})],
            meta={"reasoning_content": "先看文件再决定"},
        )
        d = message_to_dict(msg)
        assert d["reasoning_content"] == "先看文件再决定"

    def test_assistant_without_reasoning_no_field(self) -> None:
        msg = Message(role=MessageRole.ASSISTANT, content="hi")
        d = message_to_dict(msg)
        assert "reasoning_content" not in d

    def test_user_message_never_carries_reasoning_content(self) -> None:
        """破坏者视角：非 assistant 角色即便 meta 被污染也不得下发该字段。"""
        msg = Message(role=MessageRole.USER, content="hi", meta={"reasoning_content": "x"})
        d = message_to_dict(msg)
        assert "reasoning_content" not in d

    def test_restore_keeps_reasoning_content_meta(self) -> None:
        entry = {
            "role": "assistant",
            "content": "",
            "metadata": (
                '{"tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a"}}], '
                '"reasoning_content": "思考"}'
            ),
        }
        msg = message_from_entry(entry)
        assert msg.meta.get("reasoning_content") == "思考"
        assert message_to_dict(msg)["reasoning_content"] == "思考"
