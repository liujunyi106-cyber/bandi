# 功能：契约——长期记忆写入必须经过 merge_on_write 的写入侧合并。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.agent_runner import _persist_memory
from bandi.core.event_bus import EventBus
from bandi.memory.long_term import LongTermMemory


@pytest.mark.asyncio
async def test_persist_memory_uses_merge_on_write(tmp_path: Any, monkeypatch: Any) -> None:
    from bandi.memory import merge as merge_module

    calls: list[str] = []
    original = merge_module.merge_on_write

    def _spy(long_term: Any, incoming: Any, key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return original(long_term, incoming, key, **kwargs)

    monkeypatch.setattr(merge_module, "merge_on_write", _spy)
    store = LongTermMemory(str(tmp_path / "lt.db"))
    manager = SimpleNamespace(long_term=store, episodic=None, working=None)
    ctx = SimpleNamespace(
        raw_input="记住：项目代号是 X",
        turn_index=1,
        terminal_reason=TerminalReason.COMPLETED,
        final_message="已记录项目代号 X。",
        current_content="",
        tool_results=None,
        messages=[],
        meta={"session_id": "s1"},
        bus=EventBus(),
    )

    await _persist_memory(ctx, manager, "s1")

    assert calls, "长期记忆直写未经过 merge_on_write"
    store.close()
