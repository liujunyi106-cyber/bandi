# 功能：员工权限卡必须携带实际执行会话 id，allow_session 才能写给 role-session
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore


def test_permission_payload_carries_executor_session_id() -> None:
    ui = HTTPUserInteraction(
        InteractionStore(),
        session_id="chat-session",
        executor_session_id="role-session::p::w::worker",
    )
    seen: dict = {}

    async def fake_suspend(kind: str, payload: dict, timeout: float | None = None):
        seen.update(payload)
        return True

    ui._suspend = fake_suspend  # type: ignore[method-assign]
    assert asyncio.run(ui.ask_permission("bash", {"command": "ls"}, "high")) is True
    assert seen["executor_session_id"] == "role-session::p::w::worker"


def test_permission_payload_defaults_to_empty_executor() -> None:
    ui = HTTPUserInteraction(InteractionStore(), session_id="chat-session")
    ui._suspend = AsyncMock(return_value=True)  # type: ignore[method-assign]
    asyncio.run(ui.ask_permission("bash", {"command": "ls"}, "high"))
    payload = ui._suspend.await_args.args[1]  # type: ignore[attr-defined]
    assert payload["executor_session_id"] == ""
