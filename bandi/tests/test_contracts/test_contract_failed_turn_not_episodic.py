# 功能：契约——失败/返工回合不得写入情景记忆。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.agent_runner import _persist_memory
from bandi.core.event_bus import EventBus


class _Episodic:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, episode: Any) -> None:
        self.records.append(episode)


class _Manager:
    def __init__(self) -> None:
        self.episodic = _Episodic()
        self.long_term = None
        self.working = None


def _ctx(terminal: TerminalReason) -> Any:
    return SimpleNamespace(
        raw_input="用户询问项目计划",
        turn_index=1,
        terminal_reason=terminal,
        final_message="这是计划内容",
        current_content="",
        meta={},
        bus=EventBus(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [TerminalReason.REWORK, TerminalReason.ESCALATED])
async def test_failed_terminal_skips_episodic(terminal: TerminalReason) -> None:
    manager = _Manager()

    await _persist_memory(_ctx(terminal), manager, "session-1")

    assert manager.episodic.records == [], f"{terminal.name} 回合不得写情景记忆"
