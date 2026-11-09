from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bandi.cli.stream_handler import CLIStreamHandler


@pytest.fixture
def handler() -> CLIStreamHandler:
    return CLIStreamHandler(console=MagicMock())


class TestCLIStreamHandler:
    @pytest.mark.asyncio
    async def test_on_content_delta_buffers(self, handler: CLIStreamHandler) -> None:
        await handler.on_content_delta("s-1", "hello")
        assert "".join(handler._token_buffer) == "hello"

    @pytest.mark.asyncio
    async def test_on_content_delta_flush_on_punctuation(self, handler: CLIStreamHandler) -> None:
        await handler.on_content_delta("s-1", "hello.")
        assert handler._token_buffer == []

    @pytest.mark.asyncio
    async def test_on_content_delta_flush_on_newline(self, handler: CLIStreamHandler) -> None:
        await handler.on_content_delta("s-1", "hello\n")
        assert handler._token_buffer == []

    @pytest.mark.asyncio
    async def test_on_content_delta_batch_flush(self, handler: CLIStreamHandler) -> None:
        for i in range(5):
            await handler.on_content_delta("s-1", f"token{i}")
        assert handler._token_buffer == []

    @pytest.mark.asyncio
    async def test_drain_catch_up_mode(self, handler: CLIStreamHandler) -> None:
        handler._drain_mode = "catch_up"
        await handler.on_content_delta("s-1", "hello")
        assert handler._drain_count == 1

    @pytest.mark.asyncio
    async def test_drain_catch_up_resets_after_two(self, handler: CLIStreamHandler) -> None:
        handler._drain_mode = "catch_up"
        handler._drain_count = 2
        await handler.on_content_delta("s-1", "hello")
        assert handler._drain_mode == "smooth"

    @pytest.mark.asyncio
    async def test_on_tool_call_start(self, handler: CLIStreamHandler) -> None:
        await handler.on_tool_call_start("s-1", "bash")
        assert handler._tool_count == 1

    @pytest.mark.asyncio
    async def test_on_tool_result_success(self, handler: CLIStreamHandler) -> None:
        await handler.on_tool_result("s-1", "bash", True, "ok", 100)
        assert handler._console.print.called

    @pytest.mark.asyncio
    async def test_on_tool_result_error(self, handler: CLIStreamHandler) -> None:
        await handler.on_tool_result("s-1", "bash", False, "failed", 50)
        assert handler._console.print.called

    @pytest.mark.asyncio
    async def test_on_done_flushes_buffer(self, handler: CLIStreamHandler) -> None:
        handler._token_buffer = ["hello", " world"]
        handler._final_content = "hello world"
        handler._tool_count = 2
        await handler.on_done("s-1", "COMPLETED", 100, 1)
        assert handler._token_buffer == []

    @pytest.mark.asyncio
    async def test_on_done_with_non_completed(self, handler: CLIStreamHandler) -> None:
        handler._final_content = "error"
        handler._tool_count = 0
        await handler.on_done("s-1", "TOOL_LIMIT", 50, 1)
        assert handler._console.print.called

    @pytest.mark.asyncio
    async def test_on_error(self, handler: CLIStreamHandler) -> None:
        await handler.on_error("s-1", "something broke")
        assert handler._console.print.called

    @pytest.mark.asyncio
    async def test_render_markdown_valid(self, handler: CLIStreamHandler) -> None:
        await handler.render_markdown("# Hello")
        assert handler._console.print.called

    @pytest.mark.asyncio
    async def test_render_markdown_fallback_on_error(self, handler: CLIStreamHandler) -> None:
        handler._console.print.side_effect = [None, None]
        handler._console = MagicMock()
        await handler.render_markdown("plain text")
