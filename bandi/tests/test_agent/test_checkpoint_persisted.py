# 功能：Checkpoint 持久化与恢复验证

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_checkpoint_persisted_and_loadable(home: Any) -> None:
    """run_agent_turn 必须真实写入 checkpoint，且可跨实例加载。"""
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = MockProvider()
    provider.add_response(
        LLMResponse(content="回答内容足够长以触发多轮执行流程验证检查点持久化行为")
    )
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
        )
    assert result.reply
    ckpt = CheckpointStore()
    entries = ckpt.list_by_session(sid)
    assert entries, f"session {sid} 必须有关键点记录（当前 checkpoint 未落库）"
    cp = ckpt.load(entries[-1]["id"])
    assert cp is not None
    assert cp.session_id == sid
    ckpt.close()
    store.close()
