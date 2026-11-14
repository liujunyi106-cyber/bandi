# 功能：契约——轮次（用户请求）与步数（轮内模型调用）分离。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.agent.checkpoint import _STEP_SLOT, checkpoint_key
from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, TokenUsage
from bandi.memory.session.store import SessionStore


def _provider(n: int) -> Any:
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    for index in range(n):
        provider.add_response(
            LLMResponse(
                content=f"答复{index}",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=2, completion_tokens=2),
                finish_reason="stop",
            )
        )
    return provider


def test_checkpoint_key_keeps_turns_and_steps_apart() -> None:
    """存档键必须让同一轮内的不同步互不覆盖，同时保持轮间严格递增。"""
    assert checkpoint_key(1, 0) < checkpoint_key(1, 1) < checkpoint_key(1, 2)
    assert checkpoint_key(1, _STEP_SLOT - 1) < checkpoint_key(2, 0)
    assert checkpoint_key(0, 0) == 0
    # 步号溢出不得侵入下一轮
    assert checkpoint_key(3, _STEP_SLOT + 5) // _STEP_SLOT == 3


def test_two_user_requests_produce_two_turns(tmp_path: Path) -> None:
    """两次 run_agent_turn = 两个用户轮；轮次不得因轮内步数或恢复而回退。"""
    from bandi.core.agent_runner import run_agent_turn

    provider = _provider(2)
    with patch("pathlib.Path.home", return_value=tmp_path):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            first = asyncio.run(
                run_agent_turn(
                    user_input="第一个请求",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
            )
            second = asyncio.run(
                run_agent_turn(
                    user_input="第二个请求",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
            )
        assert first.turn_index == 1, "首轮必须从 1 起"
        assert second.turn_index == 2, f"第二次请求必须是第 2 轮（实际 {second.turn_index}）"
        assert first.steps >= 1 and second.steps >= 1, "每轮至少跑一步"
        store.close()


def test_episode_key_uses_turn_so_two_turns_do_not_collide(tmp_path: Path) -> None:
    """经历层以轮次为稳定键——两轮必须各自成条，不得互相覆盖。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.episodic import EpisodicStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="结论：已定位 seed 参数缺失问题并给出补丁方案",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )
    provider.add_response(
        LLMResponse(
            content="结论：根据历史记录继续处理",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            for text in ("请排查 seed 参数缺失导致的启动崩溃", "继续排查 seed 参数缺失问题"):
                asyncio.run(
                    run_agent_turn(
                        user_input=text,
                        session_id=sid,
                        session_store=store,
                        bandi_config=cfg,
                    )
                )
        epi = EpisodicStore(str(tmp_path / ".bandi" / "data" / "episodic.db"))
        episodes = epi.list_recent(limit=10)
        assert len(episodes) >= 2, f"两轮至少各一条 episode（实际 {len(episodes)}）"
        ids = {e.episode_id for e in episodes}
        assert len(ids) == len(episodes), "episode_id 必须唯一（同轮重复写才去重）"
        store.close()


@pytest.mark.asyncio
async def test_injected_turn_does_not_advance_user_turn() -> None:
    """轮内注入不得推进用户轮——只有用户新请求才是新一轮。"""
    from unittest.mock import AsyncMock, MagicMock

    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    response = MagicMock()
    response.content = "处理注入"
    response.tool_calls = []
    response.finish_reason = "stop"
    provider.complete = AsyncMock(return_value=response)
    provider.count_tokens = MagicMock(return_value=5)

    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="原始任务"),
        ],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.turn_index = 4
    ctx.current_state = "observing"
    ctx.injected_input = "中途转向"

    await AgentLoop(max_iterations=3).run(ctx)

    assert ctx.turn_index == 4, f"轮内注入不得推进用户轮（实际 {ctx.turn_index}）"
    assert ctx.step_index >= 1, "但步数必须累计"
