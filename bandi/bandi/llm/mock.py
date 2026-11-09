# 功能：MockProvider 测试 Provider（预设响应 + 流式模拟）
import json
from collections.abc import AsyncGenerator
from typing import Any

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse
from bandi.llm.base import BaseProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


class MockProvider(BaseProvider):  # 功能：Mock 提供方，预置响应队列用于单元测试
    def __init__(self, config: LLMConfig | None = None):
        if config is None:
            from bandi.core.config import LLMConfig as _LLMConfig

            config = _LLMConfig()
        super().__init__(config)
        self._responses: list[LLMResponse | type[Exception]] = []
        self.call_count = 0
        self.stream_call_count = 0

    def add_response(
        self, response: LLMResponse | type[Exception]
    ) -> None:  # 功能：向响应队列追加一个预设响应（或异常类）
        self._responses.append(response)

    def clear_responses(self) -> None:  # 功能：清空响应队列并重置调用计数
        self._responses.clear()
        self.call_count = 0
        self.stream_call_count = 0

    async def complete(  # 功能：模拟非流式补全，弹出响应队列中的下一条
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            return LLMResponse(content="Mock response")
        response = self._responses.pop(0)
        if isinstance(response, type) and issubclass(response, Exception):
            raise response("Mock error")
        return response

    async def stream(  # 功能：模拟流式补全，逐段产出 ContentDelta/ToolCall/DONE 事件
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncGenerator[StreamEvent, None]:
        self.stream_call_count += 1
        if not self._responses:
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="Mock ")
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="response")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
            return

        response = self._responses.pop(0)
        if isinstance(response, type) and issubclass(response, Exception):
            yield StreamEvent(type=StreamEventType.ERROR, error_message="Mock error")
            return

        if response.reasoning:
            yield StreamEvent(
                type=StreamEventType.THINKING_DELTA, thinking_delta=response.reasoning
            )
        if response.content:
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=response.content)
        if response.tool_calls:
            for tc in response.tool_calls:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_START,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                )
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_DELTA,
                    tool_call_id=tc.id,
                    arguments_delta=json.dumps(tc.arguments, ensure_ascii=False),
                )
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_END,
                    tool_call_id=tc.id,
                )
        yield StreamEvent(
            type=StreamEventType.DONE,
            finish_reason=response.finish_reason or "stop",
            usage=response.usage,
        )
