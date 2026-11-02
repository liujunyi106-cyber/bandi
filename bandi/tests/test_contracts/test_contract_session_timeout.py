from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse


@pytest.fixture
def slow_llm() -> Any:
    # 功能：让整 turn 的 LLM 执行真的变慢（超过 session_timeout_sec）。
    # 必须注入到生产链路真正发 LLM 请求的两条路（流式 + 非流式 fallback）——
    # 只注入外围会让该契约被"候选无 provider 立刻报错"顶上：终态变成 MODEL_ERROR，
    # 测到的是失败分类而非超时。
    from bandi.llm.strategy import CapabilityFallback

    async def _slow_complete(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("超时后不应到达 complete 返回")

    async def _slow_stream(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("超时后不应到达 stream 返回")

    with (
        patch.object(CapabilityFallback, "complete_with_fallback", side_effect=_slow_complete),
        patch("bandi.agent.states._stream_thinking", side_effect=_slow_stream),
    ):
        yield


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_timeout_produces_timeout_terminal_reason(home: Any, slow_llm: Any) -> None:
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(title="t")
    result = await run_agent_turn(
        user_input="慢慢回答",
        session_id=sid,
        session_store=store,
        session_timeout_sec=1,
    )
    assert result.terminal_reason == "TIMEOUT", (
        f"session_timeout_sec>0 必须产生 TIMEOUT 终态，实际 {result.terminal_reason}"
    )
    store.close()


@pytest.mark.asyncio
async def test_zero_timeout_disables_limit(home: Any) -> None:
    """默认 0 = 不启用整 turn 超时（现有行为不变）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(title="t")
    provider = MockProvider()
    provider.add_response(LLMResponse(content="done"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hi",
            session_id=sid,
            session_store=store,
        )
    assert result.terminal_reason != "TIMEOUT"
    store.close()
