# 功能：契约——压缩预算与收敛判定必须按真实请求口径计入 tool_calls 参数。
from __future__ import annotations

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.compression_pipeline import CompressionPipeline


def test_budget_counts_tool_call_arguments() -> None:
    message = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ToolCall(id="call-1", name="write", arguments={"content": "x" * 600}),
        ],
    )

    assert CompressionPipeline._is_under_budget([message], 10, 1.0) is False
