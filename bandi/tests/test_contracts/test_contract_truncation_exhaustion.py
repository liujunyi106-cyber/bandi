# 功能：截断恢复契约（耗尽显式标记 + 恢复请求输出上限提升）
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.states import on_thinking
from bandi.core.config import AgentConfig, BandiConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, MessageRole


class _Bus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _KwProvider:
    model_name = "stub"
    protocol = "openai"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.kwargs_seen: list[dict] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def complete(self, messages: list[dict], tools: Any = None, **kwargs: Any) -> LLMResponse:
        self.kwargs_seen.append(dict(kwargs))
        if not self._responses:
            return LLMResponse(content="done")
        return self._responses.pop(0)


def _make_ctx(provider: _KwProvider) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="hello"),
        ],
        provider=provider,  # type: ignore[arg-type]
        config=AgentConfig(context_window=128_000, compression_threshold=0.85),
        bus=_Bus(),
    )
    ctx.meta["bandi_config"] = BandiConfig()
    return ctx


@pytest.mark.asyncio
async def test_recovery_request_raises_output_budget() -> None:
    """截断恢复请求必须携带提升后的 max_tokens（×4，封顶 32768）。"""
    provider = _KwProvider(
        [
            LLMResponse(content="part-1", finish_reason="length"),
            LLMResponse(content="full answer"),
        ]
    )
    ctx = _make_ctx(provider)
    await on_thinking(ctx)
    assert len(provider.kwargs_seen) == 2, "首次 + 1 次恢复 = 2 次调用"
    assert provider.kwargs_seen[0].get("max_tokens") is None, "首次请求不得提升"
    assert provider.kwargs_seen[1].get("max_tokens") == 32768, (
        f"恢复请求必须提升输出上限（实际 {provider.kwargs_seen[1].get('max_tokens')}）"
    )


@pytest.mark.asyncio
async def test_truncation_exhausted_marks_incomplete_and_keeps_content() -> None:
    """2 次恢复后仍截断 → 内容保留 + 显式不完整标记（不静默当完整回答）。"""
    provider = _KwProvider(
        [
            LLMResponse(content="p1", finish_reason="length"),
            LLMResponse(content="p2", finish_reason="length"),
            LLMResponse(content="p3", finish_reason="length"),
        ]
    )
    ctx = _make_ctx(provider)
    await on_thinking(ctx)
    assert ctx.meta.get("truncation_exhausted") is True
    content = ctx.current_content or ""
    assert content.startswith("p3"), "截断内容必须保留"
    assert "截断" in content and "不完整" in content, f"必须附用户可见不完整提示：{content!r}"


@pytest.mark.asyncio
async def test_recovery_budget_capped_at_boundary() -> None:
    """边界：base×4 超过封顶（32768）时取封顶值。"""
    provider = _KwProvider(
        [
            LLMResponse(content="p1", finish_reason="length"),
            LLMResponse(content="p2", finish_reason="length"),
            LLMResponse(content="done"),
        ]
    )
    ctx = _make_ctx(provider)
    ctx.meta["bandi_config"].llm.max_output_tokens = 16384  # ×4 = 65536 → 封顶
    await on_thinking(ctx)
    assert provider.kwargs_seen[1].get("max_tokens") == 32768
