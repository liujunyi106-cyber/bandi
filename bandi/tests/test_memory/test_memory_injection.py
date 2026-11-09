# 功能：记忆注入感知层验证 — MemoryContextSource 结果必须进入 system prompt（单轮+多轮）

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _RecordingProvider(MockProvider):
    """记录每次 complete/stream 收到的 messages。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _make_provider() -> _RecordingProvider:
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    return provider


@pytest.mark.asyncio
async def test_memory_injected_into_system_prompt_single_turn(home: Any) -> None:
    """预置记忆 → 单轮对话 system prompt 必须包含记忆内容。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    manager.long_term.store(
        MemoryItem(
            item_id="k-stellar",
            content="恒星贸易是重要客户，下周启动项目",
            importance=0.9,
            metadata={},
        )
    )
    provider = _make_provider()
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="恒星贸易进展如何",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert result.reply
    assert provider.seen_messages, "必须调用过 LLM"
    injected = "\n".join(str(m.get("content") or "") for m in provider.seen_messages[0])
    assert "恒星贸易" in injected, (
        "记忆必须进 LLM 请求（易变上下文段；当前 pipeline 将记忆置于末尾 user 段）"
    )


@pytest.mark.asyncio
async def test_memory_injected_in_second_turn(home: Any) -> None:
    """第一轮写入的记忆 → 第二轮（同一 session，带关键词输入）system prompt 必须可检索到。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(
            content="结论：第一轮回答：恒星贸易项目已完成方案评审，预算五十万元已确认，联系人王经理将在下周一确认最终排期，整体进度百分之六十五。"
        )
    )
    provider.add_response(LLMResponse(content="第二轮回答"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="结论：以后恒星贸易客户统一走优先响应方案，预算 500000 元已确认，联系人王经理",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
        await run_agent_turn(
            user_input="恒星贸易进展如何",
            session_id=sid,
            session_store=store,
            memory_manager=manager,
        )
    assert len(provider.seen_messages) >= 2
    second_injected = "\n".join(str(m.get("content") or "") for m in provider.seen_messages[-1])
    assert "恒星贸易" in second_injected, "第二轮请求必须包含第一轮写入的记忆（多轮注入）"


@pytest.mark.asyncio
async def test_memory_source_budget_respected(home: Any) -> None:
    """
    budget 参数未使用（函数体从未引用），且 ContextItem 未设 token_estimate（默认 0）
    → ContextPlanner 贪心裁剪对 memory 项恒通过（total=0 恒 <= max_tokens）
    注入量实际只靠 15 条×200 字符硬上限兜底，4096 预算形同虚设。"""
    from bandi.context.sources.memory_source import MemoryContextSource
    from bandi.core.message import Message, MessageRole
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.memory_provider import MemoryManager

    manager = MemoryManager()
    for i in range(10):
        manager.long_term.store(
            MemoryItem(
                item_id=f"big-{i}",
                content=("长记忆内容 " * 40)[:800],
                importance=0.9,
                metadata={},
            )
        )
    source = MemoryContextSource(manager)
    message = Message(role=MessageRole.USER, content="长记忆内容")
    items = await source.collect(message, session_id="s1", budget=500)
    assert items, "有匹配记忆必须返回项"
    total_tokens = sum(i.token_estimate for i in items)
    assert total_tokens <= 500, f"collect 必须按 budget 裁剪，实际注入 {total_tokens} tokens > 500"
    assert all(i.token_estimate > 0 for i in items), "每项必须估算 token 数（不能默认 0）"
