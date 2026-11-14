# 功能：流式事件处理接口（StreamHandler Protocol，CLI/WebSocket/桌面端通用）
from __future__ import annotations

from typing import Any, Protocol


class StreamHandler(Protocol):
    """前端无关的流式事件处理器接口。CLI/WebSocket/桌面端都实现此 Protocol。"""

    async def on_content_delta(self, session_id: str, delta: str) -> None: ...

    async def on_thinking(self, session_id: str, delta: str) -> None: ...

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None: ...

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None: ...

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None: ...

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None: ...

    async def on_note(self, session_id: str, note_type: str) -> None: ...

    async def on_compaction_start(self, session_id: str) -> None: ...

    async def on_compaction(self, session_id: str, detail: dict[str, Any]) -> None: ...

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None: ...

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None: ...

    async def on_done(
        self,
        session_id: str,
        terminal_reason: str,
        total_tokens: int,
        turn_index: int,
        duration_ms: float = 0.0,
        steps: int = 0,
    ) -> None: ...

    async def on_error(self, session_id: str, error: str) -> None: ...
