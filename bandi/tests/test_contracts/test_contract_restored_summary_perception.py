# 功能：压缩摘要恢复后下一轮必须重新组装系统提示，不得把摘要当成已装配 system
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bandi.agent.loop import AgentLoop
from bandi.core.agent_runner import _session_restore
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


class _FakePerception:
    def __init__(self) -> None:
        self.calls = 0

    async def process(self, raw_input: str, **_kwargs: object) -> object:
        self.calls += 1
        return SimpleNamespace(
            messages=[
                Message(role=MessageRole.SYSTEM, content="IDENTITY PROMPT"),
                Message(role=MessageRole.USER, content=raw_input),
            ]
        )


def test_restored_summary_does_not_skip_perception(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session()
    store.append_summary_entry(session_id, 1, "[上下文摘要] 旧上下文")
    store.append_message(session_id, Message(role=MessageRole.USER, content="上一问"))
    store.append_message(session_id, Message(role=MessageRole.ASSISTANT, content="上一答"))

    ctx = SimpleNamespace(
        messages=[],
        raw_input="这一问",
        meta={},
        active_tools=None,
        bus=None,
    )
    _session_restore(session_id, store, ctx)  # type: ignore[arg-type]

    assert ctx.meta.get("restored_summary") is True
    assert ctx.messages[0].role == MessageRole.SYSTEM

    perception = _FakePerception()
    loop = AgentLoop(perception=perception)
    asyncio.run(loop._run_perception(ctx))  # type: ignore[arg-type]

    assert perception.calls == 1, "摘要恢复后必须重新跑感知/系统提示组装"
    store.close()


def test_existing_system_prompt_still_skips_perception() -> None:
    perception = _FakePerception()
    loop = AgentLoop(perception=perception)
    ctx = SimpleNamespace(
        messages=[Message(role=MessageRole.SYSTEM, content="IDENTITY")],
        raw_input="hi",
        meta={},
        active_tools=None,
        bus=None,
    )
    asyncio.run(loop._run_perception(ctx))  # type: ignore[arg-type]
    assert perception.calls == 0
