from __future__ import annotations

from bandi.core.event_bus import EventBus
from bandi.core.events import Event
from bandi.core.message import LLMResponse, ToolCall, ToolResult


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class MockProvider:
    def __init__(self):
        self.call_count = 0
        self.supports_streaming = False
        self._responses: list[LLMResponse | type[Exception]] = []

    def add_response(self, response: LLMResponse | type[Exception]) -> None:
        self._responses.append(response)

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            return LLMResponse(content="ok")
        response = self._responses.pop(0)
        if isinstance(response, type) and issubclass(response, Exception):
            raise response("mock error")
        return response

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        yield


class MockToolExecutor:
    def __init__(self):
        self._results: list[ToolResult] | None = None

    def set_results(self, results: list[ToolResult]) -> None:
        self._results = results

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        if self._results is not None:
            return self._results
        return [
            ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
            for tc in tool_calls
        ]
