# 功能：渐进压缩零丢失契约——分块压缩不得丢未摘要区段/失败块原文回填（2.1/2.2 补钉）
# 既未摘要也未保留（静默丢失）；摘要失败的块无回填（内容无痕）。
# 修复语义：keep_tail=False（整块进摘要目标）；失败块原文回填且与尾区去重。
from __future__ import annotations

import re

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compression_pipeline import ProgressiveCompressor

_TAG_RE = re.compile(r"TAG-\d+")


def _tagged_messages(count: int) -> list[Message]:
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手")]
    for i in range(count):
        msgs.append(
            Message(
                role=MessageRole.USER, content=f"TAG-{i} " + f"第{i}步执行日志与上下文内容。" * 12
            )
        )
    return msgs


def _request_tags(messages: list[dict]) -> str:
    text = "\n".join(str(m.get("content") or "") for m in messages)
    return " ".join(dict.fromkeys(_TAG_RE.findall(text)))


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        finish_reason="stop",
    )


class TestProgressiveNoSilentLoss:
    @pytest.mark.asyncio
    async def test_no_untreated_gap_between_chunks(self) -> None:
        """每个分块的输入内容必须进入摘要或保留区——不得有未处理区段静默消失。"""
        tags = [f"TAG-{i}" for i in range(12)]
        requests: list[str] = []

        async def stub(messages: list[dict]) -> LLMResponse:
            extracted = _request_tags(messages)
            requests.append(extracted)
            return _response(f"## 摘要\n- {extracted}")

        comp = ProgressiveCompressor(max_chunks=3, target_token_budget=50)
        result, stats = await comp.compress(_tagged_messages(12), stub)

        assert len(requests) >= 2, "前置：必须真实分块（多块才有多段丢失风险）"
        blob = "\n".join(m.content or "" for m in result)
        missing = [t for t in tags if t not in blob]
        assert not missing, f"分块压缩后不得有任何消息既未摘要也未保留（丢失 {missing}）"
        assert stats.progressive_chunks >= 2

    @pytest.mark.asyncio
    async def test_failed_chunk_content_backfilled(self) -> None:
        """破坏者：首块摘要失败（空返回）→ 该块原文必须回填，不得无痕丢失。"""
        calls = {"n": 0}

        async def stub(messages: list[dict]) -> LLMResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                return _response("")  # 空摘要 → 该块失败
            return _response(f"## 摘要\n- {_request_tags(messages)}")

        comp = ProgressiveCompressor(max_chunks=2, target_token_budget=50)
        result, stats = await comp.compress(_tagged_messages(10), stub)

        assert stats.progressive_failures >= 1, "前置：必须发生块摘要失败"
        blob = "\n".join(m.content or "" for m in result)
        first_chunk_tags = [f"TAG-{i}" for i in range(5)]
        present = [t for t in first_chunk_tags if t in blob]
        assert present, "失败块原文必须回填（此前整块无痕丢失）"

    @pytest.mark.asyncio
    async def test_failed_chunk_not_duplicated_with_tail(self) -> None:
        """失败块恰在尾部保留区时不得重复出现（按对象身份去重）。"""
        calls = {"n": 0}

        async def stub(messages: list[dict]) -> LLMResponse:
            calls["n"] += 1
            return _response("")

        comp = ProgressiveCompressor(max_chunks=2, target_token_budget=50)
        result, _stats = await comp.compress(_tagged_messages(8), stub)

        contents = [m.content for m in result if m.role == MessageRole.USER]
        assert len(contents) == len(set(contents)), "同一消息对象不得同时出现在回填区与尾区"
