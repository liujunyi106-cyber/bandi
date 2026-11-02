"""契约：流式中断/失败的用户可见错误文案必须为中文且可行动。

- Provider 首个事件后中途断流 → ERROR 事件 error_message 不得再透传英文
  （`states.py` 把 error_message 直接写入 `ctx.final_message` → 用户可见）。
- HTTP 适配器流式失败 → 文案须中文且带 provider 标识。
- 破坏者视角：任何把英文原样塞进 error_message 的改动都会被本契约拦下。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bandi.core.config import LLMConfig
from bandi.llm.providers._http_provider import HttpAdapterProvider
from bandi.llm.providers.openai import OpenAICompatProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


async def _broken_sse(*args: object, **kwargs: object):
    yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="你好")
    raise RuntimeError("connection reset by peer")


async def _broken_adapter(*args: object, **kwargs: object):
    yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="hi")
    raise RuntimeError("upstream boom")


class _ProbeProvider(HttpAdapterProvider):
    _provider_label = "ProbeLLM"
    _adapter_stream = staticmethod(_broken_adapter)


class TestOpenAICompatStreamInterrupt:
    @pytest.mark.asyncio
    async def test_midflight_interrupt_message_is_chinese(self) -> None:
        provider = OpenAICompatProvider(LLMConfig(default_model="deepseek-v4-flash"))
        try:
            with patch("bandi.llm.providers.openai.sse_stream", _broken_sse):
                events = [e async for e in provider.stream([{"role": "user", "content": "hi"}])]
        finally:
            await provider.close()
        assert events and events[-1].type == StreamEventType.ERROR
        msg = events[-1].error_message or ""
        assert "中断" in msg, f"断流文案须为中文且点明中断，实际：{msg!r}"
        assert not msg.isascii(), f"断流文案不得为英文，实际：{msg!r}"


class TestHttpAdapterStreamFailed:
    @pytest.mark.asyncio
    async def test_stream_failed_message_is_chinese_with_label(self) -> None:
        provider = _ProbeProvider(LLMConfig(default_model="probe-model"))
        try:
            events = [e async for e in provider.stream([{"role": "user", "content": "hi"}])]
        finally:
            await provider.close()
        assert events and events[-1].type == StreamEventType.ERROR
        msg = events[-1].error_message or ""
        assert "失败" in msg, f"适配器失败文案须为中文且点明失败，实际：{msg!r}"
        assert "ProbeLLM" in msg, f"失败文案须带 provider 标识，实际：{msg!r}"
