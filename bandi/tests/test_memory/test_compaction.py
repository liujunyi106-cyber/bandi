import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compaction import MemoryCompactor


async def _mock_complete(messages):
    return LLMResponse(
        content="这是摘要",
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
        finish_reason="stop",
    )


class TestMemoryCompactor:
    def test_truncate_keeps_system(self) -> None:
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="a"),
            Message(role=MessageRole.ASSISTANT, content="b"),
            Message(role=MessageRole.USER, content="c"),
        ]
        result = MemoryCompactor.truncate(msgs, min_keep=2)
        assert result[0].role == MessageRole.SYSTEM
        assert len(result) == 3

    def test_truncate_empty_non_system(self) -> None:
        msgs = [Message(role=MessageRole.SYSTEM, content="sys")]
        result = MemoryCompactor.truncate(msgs)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_compact_with_few_messages(self) -> None:
        compactor = MemoryCompactor()
        msgs = [Message(role=MessageRole.USER, content="hi")]
        _res, success, inc = await compactor.compact(msgs, _mock_complete)
        assert success is False
        assert inc == 1

    @pytest.mark.asyncio
    async def test_compact_success(self) -> None:
        compactor = MemoryCompactor(target_token_budget=1)
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="hello"),
            Message(role=MessageRole.ASSISTANT, content="world"),
            Message(role=MessageRole.USER, content="how are you"),
        ]
        result, success, _inc = await compactor.compact(msgs, _mock_complete)
        assert success is True
        assert len(result) <= len(msgs) + 1
