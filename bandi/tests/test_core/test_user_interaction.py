from __future__ import annotations

from typing import Any

import pytest

from bandi.core.user_interaction import UserInteraction


class _MockInteraction:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        self.calls.append(f"permission:{tool_name}:{risk}")
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        self.calls.append(f"confirm:{prompt}")
        return True

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        self.calls.append(f"input:{prompt}:{secret}")
        return "user input"

    async def show_notification(self, message: str, level: str = "info") -> None:
        self.calls.append(f"notify:{level}:{message}")


class TestUserInteractionProtocol:
    @pytest.mark.asyncio
    async def test_protocol_duck_type(self) -> None:
        ui: UserInteraction = _MockInteraction()
        result = await ui.ask_permission("bash", {"cmd": "ls"}, "high")
        assert result is True

    @pytest.mark.asyncio
    async def test_ask_confirmation(self) -> None:
        ui: UserInteraction = _MockInteraction()
        assert await ui.ask_confirmation("Are you sure?") is True

    @pytest.mark.asyncio
    async def test_ask_input_regular(self) -> None:
        ui: UserInteraction = _MockInteraction()
        result = await ui.ask_input("Enter name:")
        assert result == "user input"

    @pytest.mark.asyncio
    async def test_ask_input_secret(self) -> None:
        ui: UserInteraction = _MockInteraction()
        result = await ui.ask_input("Enter password:", secret=True)
        assert result == "user input"

    @pytest.mark.asyncio
    async def test_show_notification(self) -> None:
        ui: UserInteraction = _MockInteraction()
        await ui.show_notification("Done", level="info")
        await ui.show_notification("Error", level="error")
        assert len(ui.calls) == 2
