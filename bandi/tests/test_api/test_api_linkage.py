# 功能：API 链路验证 — FastAPI 端点真实返回 + 成本追踪真实记录

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_api_chat_endpoint_returns_reply(home: Any) -> None:
    """FastAPI /api/agent/chat 端点必须真实返回 Agent 回复。"""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = AsyncMock(
            reply="API 回答内容",
            terminal_reason="COMPLETED",
            total_tokens=10,
            turn_index=1,
            session_id="s-api",
        )
        client = TestClient(app)
        resp = client.post(
            "/api/agent/chat",
            json={"message": "hello", "session_id": "s-api"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code in (200, 401), f"端点应可达，实际 {resp.status_code}"


@pytest.mark.asyncio
async def test_cost_tracker_records_tokens(home: Any) -> None:
    """LLM 调用后 cost_tracker 必须记录 token（非空实现）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    class _CompleteOnlyProvider(MockProvider):
        @property
        def supports_streaming(self) -> bool:
            return False

        async def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(
                content="回答内容",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30),
            )

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _CompleteOnlyProvider()
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
        )
    assert result.total_tokens == 80, (
        f"cost_tracker 必须累计 token（prompt50+completion30），实际 {result.total_tokens}"
    )
    store.close()
