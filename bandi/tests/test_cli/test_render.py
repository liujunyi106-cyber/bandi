from __future__ import annotations

from unittest.mock import MagicMock

from bandi.cli.render import render_explain


class _MockSessionStore:
    def __init__(self, entries: list[dict] | None = None) -> None:
        self._entries = entries or []

    def get_branch(self, session_id: str) -> list[dict]:
        return self._entries

    def resolved_branch(self, session_id: str, at: str | None = None) -> list[dict]:
        if at is None:
            return self._entries
        idx = next((i for i, e in enumerate(self._entries) if e.get("id") == at), -1)
        if idx == -1:
            raise ValueError(f"Entry {at} not found in session {session_id}")
        return self._entries[: idx + 1]


class TestRenderExplain:
    def test_render_empty_trajectory(self) -> None:
        store = _MockSessionStore([])
        console = MagicMock()
        render_explain("s-1", store, console)
        console.print.assert_called_once()

    def test_render_with_assistant_message(self) -> None:
        store = _MockSessionStore(
            [
                {
                    "entry_type": "message",
                    "role": "assistant",
                    "content": "Hello!",
                    "turn_index": 0,
                    "metadata": "{}",
                },
            ]
        )
        console = MagicMock()
        render_explain("s-1", store, console)
        console.print.assert_called()

    def test_render_with_tool_calls(self) -> None:
        store = _MockSessionStore(
            [
                {
                    "entry_type": "message",
                    "role": "assistant",
                    "content": "Let me check",
                    "turn_index": 1,
                    "metadata": '{"tool_calls": [{"name": "bash", "arguments": {"cmd": "ls"}}]}',
                },
            ]
        )
        console = MagicMock()
        render_explain("s-1", store, console)
        console.print.assert_called()

    def test_render_malformed_metadata(self) -> None:
        store = _MockSessionStore(
            [
                {
                    "entry_type": "message",
                    "role": "assistant",
                    "content": "Hi",
                    "turn_index": 0,
                    "metadata": "not json",
                },
            ]
        )
        console = MagicMock()
        render_explain("s-1", store, console)
        console.print.assert_called()
