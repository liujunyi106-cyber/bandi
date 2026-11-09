# 功能：DocClassifierTool 测试 — LLM 调用契约（messages 列表 + content 解析）

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.tools.doc.doc_classifier import DocClassifierTool


class _RecordingProvider(MockProvider):
    """MockProvider 变体：记录 complete 收到的 messages。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)


class TestLLMMethodContract:
    @pytest.mark.asyncio
    async def test_complete_receives_message_list_not_string(self) -> None:
        provider = _RecordingProvider()
        provider.add_response(LLMResponse(content="invoice"))
        tool = DocClassifierTool(provider=provider)
        result = await tool.execute({"text": "Invoice #123 total 500 usd", "method": "llm"})
        assert result["success"] is True
        assert result["category"] == "invoice"
        assert provider.seen_messages, "LLM 路径必须调用 complete"
        for msgs in provider.seen_messages:
            assert isinstance(msgs, list), "complete 必须收到消息列表"
            assert all(isinstance(m, dict) for m in msgs), "每条消息必须是 dict"
            assert msgs[0]["role"] == "user", "首条消息 role 应为 user"
            assert "Invoice #123 total 500 usd" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_predicted_from_content_not_repr(self) -> None:
        provider = _RecordingProvider()
        provider.add_response(LLMResponse(content="contract"))
        tool = DocClassifierTool(provider=provider)
        result = await tool.execute({"text": "Party A agrees to pay Party B", "method": "llm"})
        assert result["success"] is True
        assert result["category"] == "contract", (
            "应从 response.content 解析分类，而非 dataclass repr"
        )

    @pytest.mark.asyncio
    async def test_llm_error_falls_back_gracefully(self) -> None:
        provider = _RecordingProvider()
        provider.add_response(LLMResponse(content="", refusal=True))
        tool = DocClassifierTool(provider=provider)
        result = await tool.execute({"text": "some random text", "method": "llm"})
        assert result["success"] is True
        assert result["category"] == "other"

    @pytest.mark.asyncio
    async def test_hybrid_rules_fallback_no_provider(self, tmp_path: Any) -> None:
        tool = DocClassifierTool(provider=None)
        result = await tool.execute(
            {"text": "definitely an invoice with amount due 100", "method": "hybrid"}
        )
        assert result["success"] is True
        assert result["method_used"] in ("rules", "rules (fallback)")
