from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bandi.cli.user_interaction import CLIUserInteraction


class TestCLIUserInteraction:
    @pytest.mark.asyncio
    async def test_ask_permission_yes(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="y"):
            ui = CLIUserInteraction(console=MagicMock())
            assert await ui.ask_permission("bash", {"command": "ls"}, "high") is True

    @pytest.mark.asyncio
    async def test_ask_permission_no(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="n"):
            ui = CLIUserInteraction(console=MagicMock())
            assert await ui.ask_permission("write", {"file_path": "/tmp/x"}, "medium") is False

    @pytest.mark.asyncio
    async def test_ask_permission_always(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="a"):
            ui = CLIUserInteraction(console=MagicMock())
            assert await ui.ask_permission("bash", {"command": "ls"}, "high") is True

    @pytest.mark.asyncio
    async def test_ask_confirmation_yes(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="y"):
            ui = CLIUserInteraction(console=MagicMock())
            assert await ui.ask_confirmation("确认删除？") is True

    @pytest.mark.asyncio
    async def test_ask_confirmation_no(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="n"):
            ui = CLIUserInteraction(console=MagicMock())
            assert await ui.ask_confirmation("确认？") is False

    @pytest.mark.asyncio
    async def test_ask_input(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="my input"):
            ui = CLIUserInteraction(console=MagicMock())
            result = await ui.ask_input("请输入")
            assert result == "my input"

    @pytest.mark.asyncio
    async def test_ask_input_secret(self) -> None:
        with patch("bandi.cli.user_interaction.Prompt.ask", return_value="secret123"):
            ui = CLIUserInteraction(console=MagicMock())
            result = await ui.ask_input("密码", secret=True)
            assert result == "secret123"

    @pytest.mark.asyncio
    async def test_show_notification_info(self) -> None:
        console = MagicMock()
        ui = CLIUserInteraction(console=console)
        await ui.show_notification("test message", level="info")
        console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_show_notification_error(self) -> None:
        console = MagicMock()
        ui = CLIUserInteraction(console=console)
        await ui.show_notification("error msg", level="error")
        console.print.assert_called_once()
