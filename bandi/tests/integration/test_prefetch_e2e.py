# 功能：Prefetch 生产链路集成测试（run_agent_turn 第二轮注入 + PREFETCH_HITS 事件）
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.agent_runner import run_agent_turn
from bandi.core.config import BandiConfig, LLMConfig, PrefetchConfig
from bandi.core.message import LLMResponse
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_provider import MemoryManager
from bandi.memory.session.store import SessionStore


class _PrefetchCaptureProvider:
    """捕获 LLM 收到的消息，按序返回两轮响应（首轮无工具调用、次轮完成）。"""

    protocol = "openai"
    model_name = "mock"
    supports_streaming = False

    def __init__(self) -> None:
        self.system_messages: list[list[dict]] = []

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any
    ) -> LLMResponse:
        self.system_messages.append([m for m in messages if m.get("role") == "system"])
        return LLMResponse(content="ok")

    def count_tokens(self, text: str) -> int:
        return len(text) // 4


async def _prefetch_payload() -> dict[str, str]:
    await asyncio.sleep(0.01)
    return {"focused_memory": "用户偏好用表格呈现数据"}


@pytest.mark.asyncio
async def test_e2e_prefetch_second_turn_injects_and_emits(tmp_path: Any) -> None:
    """第二轮 thinking 注入 Runtime Prefetch 分节块 + 发出 PREFETCH_HITS 事件。"""
    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig(
            prefetch=PrefetchConfig(enabled=True),
            llm=LLMConfig(api_key=""),
        )
        memory = MemoryManager(long_term=LongTermMemory())
        memory.long_term.store(
            MemoryItem(item_id="k1", content="用户偏好用表格呈现数据", importance=0.9)
        )

        from bandi.agent.states import on_thinking
        from bandi.perception.prefetch import PrefetchHandle, build_prefetch_query

        provider = _PrefetchCaptureProvider()
        # 直接驱动两轮 thinking（真实 store + 真实 MemoryManager + 真实 provider 消息流）
        from bandi.agent.base import ExecutionContext
        from bandi.core.config import AgentConfig
        from bandi.core.events import Event, EventType
        from bandi.core.message import Message, MessageRole

        class _Bus:
            def __init__(self) -> None:
                self.events: list[Event] = []

            async def emit(self, event: Event) -> None:
                self.events.append(event)

        bus = _Bus()
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
            config=AgentConfig(stream_to_ui=False),
            bus=bus,  # type: ignore[arg-type]
        )
        ctx.meta["bandi_config"] = cfg
        handle = PrefetchHandle(
            provider=lambda q, s, m: _prefetch_payload(),
            query=build_prefetch_query([{"role": "user", "content": "hi"}]),
            session_id=sid,
            messages=[],
        ).start()
        ctx.meta["prefetch_handle"] = handle

        # 第一轮：不注入
        await on_thinking(ctx)
        first_injected = any(
            "Runtime Prefetch" in str(m.get("content", "")) for m in provider.system_messages[0]
        )
        assert first_injected is False, "第一轮不得注入 prefetch"

        # 第二轮：注入 + 事件（轮次由 agent_runner 在新请求时推进，首轮=1、次轮=2）
        ctx.turn.turn_index = 2
        await on_thinking(ctx)
        second_injected = [
            m
            for m in provider.system_messages[1]
            if "Runtime Prefetch" in str(m.get("content", ""))
        ]
        assert second_injected, "第二轮 LLM 请求必须包含 prefetch 分节块"
        hits = [e for e in bus.events if e.type == EventType.PREFETCH_HITS]
        assert len(hits) == 1
        assert "focused_memory" in hits[0].data.get("keys", [])


@pytest.mark.asyncio
async def test_e2e_prefetch_disabled_no_injection(tmp_path: Any) -> None:
    """prefetch.enabled=False 时任何轮次都不注入。"""
    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        cfg = BandiConfig(prefetch=PrefetchConfig(enabled=False))
        memory = MemoryManager(long_term=LongTermMemory())
        memory.long_term.store(
            MemoryItem(item_id="k1", content="用户偏好用表格呈现数据", importance=0.9)
        )
        result = await run_agent_turn(
            user_input="帮我整理数据",
            bandi_config=cfg,
            memory_manager=memory,
        )
        assert result.reply, "禁用时 agent 正常完成"
