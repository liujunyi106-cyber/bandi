from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.agent_runner import run_agent_turn
from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, ToolCall
from bandi.llm.mock import MockProvider
from bandi.memory.session.store import SessionStore
from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig


def _todo_write_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="todo_write",
                arguments={
                    "todos": [
                        {"title": "检查 A", "status": "in_progress"},
                        {"title": "检查 B", "status": "pending"},
                    ]
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_e2e_todo_write_persists_and_restores(tmp_path: Any) -> None:
    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 4

        provider = MockProvider(cfg.llm)
        provider.add_response(_todo_write_response())
        provider.add_response(LLMResponse(content="任务清单已写入"))
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            result = await run_agent_turn(
                user_input="帮我建个任务清单",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
        assert result.reply is not None

        session = store.get_session(sid)
        assert session is not None
        meta = json.loads(session["metadata"])
        ledger_items = meta["task_ledger"]
        assert [item["title"] for item in ledger_items] == ["检查 A", "检查 B"]
        assert ledger_items[0]["status"] == "in_progress"

        restored = TaskLedger(config=TaskLedgerConfig(), session_id=sid, session_store=store)
        restored.restore()
        assert [item["title"] for item in restored.items] == ["检查 A", "检查 B"]

        provider2 = MockProvider(cfg.llm)
        provider2.add_response(LLMResponse(content="已读取任务"))
        with patch("bandi.core.agent_runner.build_provider", return_value=provider2):
            result2 = await run_agent_turn(
                user_input="读一下任务",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
        assert result2.reply is not None
        session2 = store.get_session(sid)
        assert session2 is not None
        meta2 = json.loads(session2["metadata"])
        assert meta2["task_ledger"][0]["title"] == "检查 A"
        store.close()
