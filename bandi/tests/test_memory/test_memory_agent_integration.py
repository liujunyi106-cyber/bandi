# 功能：记忆整链接线集成测试 — run_agent_turn 写入/检索注入/跨轮持久化

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


def _recording_provider() -> MockProvider:
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="结论：根据图谱，恒星贸易项目下周启动，联系人王经理，预算五十万，已完成三个里程碑，整体进度约百分之六十五。"
        )
    )
    return provider


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_run_agent_turn_writes_long_term(home: Any) -> None:
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    from bandi.core.agent_runner import run_agent_turn

    with patch("bandi.core.agent_runner.build_provider", return_value=_recording_provider()):
        result = await run_agent_turn(
            user_input="记住：我们项目的恒星贸易客户都统一走优先响应方案，联系人王经理，预算 500000 元",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert result.reply
    assert manager.long_term.count() >= 1, "用户陈述事实应写入长期记忆"


@pytest.mark.asyncio
async def test_greeting_never_writes_long_term(home: Any) -> None:
    """纯问候（无事实内容）不写长期记忆——回复侧永不直写。"""
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    provider = MockProvider()
    provider.add_response(LLMResponse(content="好的"))
    from bandi.core.agent_runner import run_agent_turn

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hi",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert result.reply
    assert manager.long_term.count() == 0, "纯问候不写长期记忆（回复侧永不直写）"


@pytest.mark.asyncio
async def test_memory_context_source_injected(home: Any) -> None:
    """预置记忆 → 下一轮感知层应能检索到（MemoryContextSource 注册生效）。"""
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    manager.long_term.store(
        MemoryItem(
            item_id="k1",
            content="恒星贸易项目下周启动，联系人王经理",
            importance=0.8,
            metadata={},
        )
    )
    provider = MockProvider()
    provider.add_response(LLMResponse(content="好的"))
    from bandi.core.agent_runner import run_agent_turn

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="恒星贸易进展如何",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert result.reply
    assert manager.long_term.count() == 1, "预置记忆不应被清空"


class TestMemoryTokenizeCn:
    def test_empty_query_returns_empty(self) -> None:
        from bandi.memory.episodic import tokenize_cn as _tokenize_cn

        assert _tokenize_cn("") == []

    def test_single_char_returns_empty(self) -> None:
        from bandi.memory.episodic import tokenize_cn as _tokenize_cn

        assert _tokenize_cn("好") == []

    def test_short_query_returns_whole(self) -> None:
        from bandi.memory.episodic import tokenize_cn as _tokenize_cn

        tokens = _tokenize_cn("客户")
        assert "客户" in tokens

    def test_long_query_contains_key_grams(self) -> None:
        from bandi.memory.episodic import tokenize_cn as _tokenize_cn

        tokens = _tokenize_cn("恒星贸易进展如何")
        assert "恒星贸易" in tokens, "4-gram 应优先切出"
        assert "贸易" in tokens or "进展" in tokens

    def test_english_query_falls_back_to_whole(self) -> None:
        from bandi.memory.episodic import tokenize_cn as _tokenize_cn

        tokens = _tokenize_cn("hello world")
        assert not tokens, "纯英文 n-gram 无意义，回退由调用方用原串检索"


@pytest.mark.asyncio
async def test_memory_persistence_across_turns(home: Any) -> None:
    """同一 session 两轮：第一轮写入 → 第二轮感知层可检索到。"""
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    manager.long_term.store(
        MemoryItem(
            item_id="seed",
            content="恒星贸易是重要客户",
            importance=0.9,
            metadata={},
        )
    )
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="结论：第一轮回答：恒星贸易项目已确认合同金额五十万元，联系人王经理，下一步确认交付日期，议价空间百分之七十五。"
        )
    )
    provider.add_response(LLMResponse(content="第二轮回答"))
    from bandi.core.agent_runner import run_agent_turn

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="记住：我们项目的恒星贸易客户都统一走优先响应方案，联系人王经理，预算 500000 元",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
        await run_agent_turn(
            user_input="继续",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert manager.long_term.count() >= 2, "第一轮长回答+预置都应保留"
