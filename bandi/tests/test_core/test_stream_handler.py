from __future__ import annotations

from typing import Any

import pytest

from bandi.core.stream_handler import StreamHandler


class _MockHandler:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        self.events.append(f"delta:{delta}")

    async def on_thinking(self, session_id: str, delta: str) -> None:
        self.events.append(f"thinking:{delta}")

    async def on_tool_call_start(
        self, session_id: str, tool_name: str, tool_input: dict[str, Any] | None = None
    ) -> None:
        self.events.append(f"tool_start:{tool_name}")

    async def on_tool_result(
        self, session_id: str, tool_name: str, success: bool, output_preview: str, duration_ms: int
    ) -> None:
        self.events.append(f"tool_result:{tool_name}:{success}")

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        self.events.append(f"memory_written:{len(channels)}")

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        self.events.append(f"task_ledger:{len(items)}")

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None:
        self.events.append(f"done:{terminal_reason}")

    async def on_error(self, session_id: str, error: str) -> None:
        self.events.append(f"error:{error}")


class TestStreamHandlerProtocol:
    @pytest.mark.asyncio
    async def test_protocol_duck_type(self) -> None:
        handler: StreamHandler = _MockHandler()
        await handler.on_content_delta("s-1", "hello")
        assert handler.events == ["delta:hello"]

    @pytest.mark.asyncio
    async def test_all_methods_invokable(self) -> None:
        handler: StreamHandler = _MockHandler()
        await handler.on_content_delta("s-1", "a")
        await handler.on_thinking("s-1", "hmm")
        await handler.on_tool_call_start("s-1", "bash")
        await handler.on_tool_result("s-1", "bash", True, "ok", 100)
        await handler.on_memory_written("s-1", ["long_term", "episodic"])
        await handler.on_task_ledger("s-1", [{"content": "a"}])
        await handler.on_done("s-1", "COMPLETED", 10, 1)
        await handler.on_error("s-1", "err")
        assert len(handler.events) == 8
