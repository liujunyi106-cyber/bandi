# 功能：P0-1 小窗口渐进压缩契约——32K/64K 窗口不得退化为 truncate
from __future__ import annotations

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compression_pipeline import ProgressiveCompressor
from bandi.utils.token_counter import TokenCounter


def _long_messages() -> list[Message]:
    messages: list[Message] = []
    for index in range(30):
        if index == 15:
            body = "head " * 200 + " MIDDLE_MARKER_15 " + "tail " * 200
        else:
            body = f"turn {index} " + "alpha " * 1200
        messages.append(Message(role=MessageRole.USER, content=body))
    return messages


@pytest.mark.asyncio
async def test_small_window_progressive_summarizes_instead_of_truncate() -> None:
    prompts: list[str] = []

    async def _llm(messages: list[dict]) -> LLMResponse:
        prompts.append(str(messages[-1].get("content") or ""))
        return LLMResponse(
            content="## 目标\n继续\n## 进度\n已完成\n## 下一步\n继续",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    messages = _long_messages()
    total = sum(TokenCounter.count(m.content or "") for m in messages)
    assert total > 30000

    compressor = ProgressiveCompressor(target_token_budget=16000, max_chunks=4)
    output, stats = await compressor.compress(messages, _llm, context_window=32000)

    assert stats.stage == "progressive"
    assert stats.progressive_chunks > 0, "小窗口必须真的生成摘要，而不是每块 bail"
    assert stats.progressive_failures == 0
    assert stats.forced_small_chunks > 0
    assert any("MIDDLE_MARKER_15" in prompt for prompt in prompts), (
        "小窗口强制摘要必须覆盖块中段内容，不能只保留头尾"
    )
    output_tokens = sum(TokenCounter.count(m.content or "") for m in output)
    assert output_tokens < total, "渐进压缩必须真实缩小上下文"


@pytest.mark.asyncio
async def test_small_window_64k_also_forces_progressive() -> None:
    async def _llm(messages: list[dict]) -> LLMResponse:
        return LLMResponse(
            content="## 目标\n继续\n## 进度\n已完成",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    messages = _long_messages()
    compressor = ProgressiveCompressor(target_token_budget=16000, max_chunks=4)
    _, stats = await compressor.compress(messages, _llm, context_window=64000)
    assert stats.progressive_chunks > 0
    assert stats.progressive_failures == 0
    assert stats.forced_small_chunks > 0


@pytest.mark.asyncio
async def test_large_window_does_not_set_forced_flag() -> None:
    async def _llm(messages: list[dict]) -> LLMResponse:
        return LLMResponse(
            content="## 目标\n继续",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    messages = _long_messages()
    compressor = ProgressiveCompressor(target_token_budget=16000, max_chunks=4)
    _, stats = await compressor.compress(messages, _llm, context_window=128000)
    # 大窗口路径行为保持原样：不触发小窗口强制摘要
    assert stats.forced_small_chunks == 0
