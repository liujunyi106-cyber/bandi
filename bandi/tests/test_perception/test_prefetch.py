# 功能：Prefetch Provider 测试（PrefetchHandle 超时语义/幂等/分轮注入/PREFETCH_HITS）

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.core.config import AgentConfig, PrefetchConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_provider import MemoryManager
from bandi.perception.prefetch import (
    MemoryPrefetchProvider,
    PrefetchHandle,
    build_prefetch_query,
    render_prefetch_block,
)


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:  # type: ignore[override]
        self.events.append(event)


async def _instant_provider(query: str, session_id: str, messages: list[dict]) -> dict[str, str]:
    return {"focused_memory": "用户喜欢表格", "session_memory": "昨日讨论过预算"}


class TestBuildPrefetchQuery:
    def test_uses_last_two_user_assistant(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答"},
            {"role": "user", "content": "第二个问题"},
            {"role": "assistant", "content": "第二个回答"},
        ]
        q = build_prefetch_query(messages)
        assert "第一个问题" not in q
        assert "第二个问题" in q
        assert "第二个回答" in q

    def test_truncates_to_800_chars(self) -> None:
        messages = [{"role": "user", "content": "x" * 2000}]
        q = build_prefetch_query(messages)
        assert len(q) == 800

    def test_ignores_tool_and_empty(self) -> None:
        messages = [
            {"role": "user", "content": ""},
            {"role": "tool", "content": "out"},
            {"role": "assistant", "content": "keep"},
        ]
        q = build_prefetch_query(messages)
        assert q == "keep"


class TestPrefetchHandleConsume:
    @pytest.mark.asyncio
    async def test_consume_success_returns_payload(self) -> None:
        handle = PrefetchHandle(
            provider=_instant_provider, query="q", session_id="s1", messages=[]
        ).start()
        payload = await handle.consume(timeout=1.0)
        assert payload["focused_memory"] == "用户喜欢表格"
        assert "session_memory" in payload

    @pytest.mark.asyncio
    async def test_consume_timeout_returns_empty_and_not_cancelled(self) -> None:
        async def slow_provider(
            query: str, session_id: str, messages: list[dict]
        ) -> dict[str, str]:
            await asyncio.sleep(0.2)
            return {"focused_memory": "迟到结果"}

        handle = PrefetchHandle(
            provider=slow_provider, query="q", session_id="s1", messages=[]
        ).start()
        payload = await handle.consume(timeout=0.01)
        assert payload == {}
        assert handle.task is not None and not handle.task.done(), "超时不应取消后台任务"
        # 超时不缓存：再次等待可拿到结果
        payload2 = await handle.consume(timeout=1.0)
        assert payload2["focused_memory"] == "迟到结果"

    @pytest.mark.asyncio
    async def test_consume_exception_caches_empty(self) -> None:
        calls: list[bool] = []

        async def failing_provider(
            query: str, session_id: str, messages: list[dict]
        ) -> dict[str, str]:
            calls.append(True)
            raise RuntimeError("memory unavailable")

        handle = PrefetchHandle(
            provider=failing_provider, query="q", session_id="s1", messages=[]
        ).start()
        payload = await handle.consume(timeout=1.0)
        assert payload == {}
        # 异常缓存：第二次 consume 直接返回空，不再重复调用 provider
        payload2 = await handle.consume(timeout=1.0)
        assert payload2 == {}
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        calls: list[int] = []

        async def counting_provider(
            query: str, session_id: str, messages: list[dict]
        ) -> dict[str, str]:
            calls.append(len(calls))
            return {"focused_memory": "x"}

        handle = PrefetchHandle(provider=counting_provider, query="q", session_id="s1", messages=[])
        handle.start()
        handle.start()
        await handle.consume(timeout=1.0)
        assert len(calls) == 1, "start() 幂等，provider 只应被调用一次"

    @pytest.mark.asyncio
    async def test_cancel_explicit(self) -> None:
        async def hanging_provider(
            query: str, session_id: str, messages: list[dict]
        ) -> dict[str, str]:
            await asyncio.sleep(10)
            return {"focused_memory": "x"}

        handle = PrefetchHandle(
            provider=hanging_provider, query="q", session_id="s1", messages=[]
        ).start()
        handle.cancel()
        await asyncio.sleep(0)
        assert handle.task is not None and handle.task.cancelled()

    @pytest.mark.asyncio
    async def test_consume_without_start_returns_empty(self) -> None:
        handle = PrefetchHandle(provider=_instant_provider, query="q", session_id="s1", messages=[])
        payload = await handle.consume(timeout=0.01)
        assert payload == {}


class TestRenderPrefetchBlock:
    def test_renders_sections(self) -> None:
        block = render_prefetch_block(
            {"focused_memory": "用户喜欢表格", "session_memory": "昨日讨论过预算"}
        )
        assert block.startswith("## Runtime Prefetch")
        assert "## focused_memory" in block
        assert "用户喜欢表格" in block

    def test_empty_payload_renders_empty(self) -> None:
        assert render_prefetch_block({}) == ""


class TestMemoryPrefetchProvider:
    def test_payload_sections_and_clip(self) -> None:
        memory = LongTermMemory()
        memory.store(MemoryItem(item_id="m1", content="quality query data " * 30, importance=0.9))
        manager = MemoryManager(long_term=memory)
        provider = MemoryPrefetchProvider(manager, max_chars=50)
        payload = asyncio.run(provider("query", "s1", []))
        assert "focused_memory" in payload, "long_term 命中必须产出 focused_memory 分节"
        for v in payload.values():
            assert len(v) <= 50, "每个分节必须被 clip 到 max_chars"

    def test_empty_memory_returns_empty(self) -> None:
        manager = MemoryManager(long_term=LongTermMemory())
        provider = MemoryPrefetchProvider(manager)
        payload = asyncio.run(provider("q", "s1", []))
        assert payload == {}

    def test_dimension_propagated_to_search_all(self) -> None:
        """
        memory_source 路径，prefetch 若不传维度，`_scope_clause` 补 IS NULL 只命中
        全局层，公司链路读不到员工×项目维度记忆（写读半隔离）。"""
        calls: list[dict[str, Any]] = []

        class _DimMemory:
            def search_all(
                self,
                query: str,
                limit: int = 5,
                project_id: str | None = None,
                employee_id: str | None = None,
            ) -> dict[str, list[Any]]:
                calls.append(
                    {
                        "query": query,
                        "limit": limit,
                        "project_id": project_id,
                        "employee_id": employee_id,
                    }
                )
                return {"long_term": []}

        provider = MemoryPrefetchProvider(_DimMemory(), project_id="proj-1", employee_id="emp-1")
        asyncio.run(provider("q", "s1", []))
        assert calls, "prefetch 必须调用 search_all"
        assert calls[0]["project_id"] == "proj-1"
        assert calls[0]["employee_id"] == "emp-1"

    def test_dimension_optional_backward_compatible(self) -> None:
        """不带维度构造必须向后兼容（单机/CLI 场景，search_all 补 IS NULL 命中全局层）。"""
        calls: list[dict[str, Any]] = []

        class _DimMemory:
            def search_all(
                self,
                query: str,
                limit: int = 5,
                project_id: str | None = None,
                employee_id: str | None = None,
            ) -> dict[str, list[Any]]:
                calls.append({"project_id": project_id, "employee_id": employee_id})
                return {"long_term": []}

        provider = MemoryPrefetchProvider(_DimMemory())
        asyncio.run(provider("q", "s1", []))
        assert calls[0]["project_id"] is None
        assert calls[0]["employee_id"] is None


class TestOnThinkingWiring:
    def _make_ctx(
        self, provider: Any, prefetch_config: PrefetchConfig | None = None
    ) -> ExecutionContext:
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
            config=AgentConfig(stream_to_ui=False),
            bus=RecordingBus(),
        )
        if prefetch_config is not None:
            from bandi.core.config import BandiConfig

            cfg = BandiConfig(prefetch=prefetch_config)
            ctx.meta["bandi_config"] = cfg
            if prefetch_config.enabled:
                handle = PrefetchHandle(
                    provider=_instant_provider,
                    query=build_prefetch_query([{"role": "user", "content": "hi"}]),
                    session_id="s1",
                    messages=[],
                ).start()
                ctx.meta["prefetch_handle"] = handle
        return ctx

    class RecordingProvider:
        def __init__(self) -> None:
            self.last_messages: list[dict] = []
            self.model_name = "mock"
            self.protocol = "openai"
            self.supports_streaming = False

        async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
            self.last_messages = messages
            return LLMResponse(content="ok")

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

    @pytest.mark.asyncio
    async def test_first_turn_no_prefetch_injection(self) -> None:
        from bandi.agent.states import on_thinking

        provider = self.RecordingProvider()
        ctx = self._make_ctx(provider, PrefetchConfig(enabled=True))
        # 首轮 turn_index=1，不注入
        await on_thinking(ctx)
        injected = any(
            m.get("role") == "system" and "## Runtime Prefetch" in str(m.get("content", ""))
            for m in provider.last_messages
        )
        assert injected is False

    @pytest.mark.asyncio
    async def test_second_turn_injects_prefetch_and_emits_event(self) -> None:
        from bandi.agent.states import on_thinking

        provider = self.RecordingProvider()
        ctx = self._make_ctx(provider, PrefetchConfig(enabled=True))
        # 轮次由 agent_runner 在新用户请求时推进（首轮=1），第二轮即 turn_index=2
        ctx.turn.turn_index = 2
        await on_thinking(ctx)
        injected = [
            m for m in provider.last_messages if "## Runtime Prefetch" in str(m.get("content", ""))
        ]
        assert injected, "第二轮 LLM 请求必须包含 prefetch 分节块"
        hits = [e for e in ctx.bus.events if e.type == EventType.PREFETCH_HITS]
        assert len(hits) == 1
        assert "focused_memory" in hits[0].data.get("keys", [])

    @pytest.mark.asyncio
    async def test_disabled_config_never_injects(self) -> None:
        from bandi.agent.states import on_thinking

        provider = self.RecordingProvider()
        ctx = self._make_ctx(provider, PrefetchConfig(enabled=False))
        # 用第二轮，确保断言的是「开关关闭」而非「首轮不注入」
        ctx.turn.turn_index = 2
        await on_thinking(ctx)
        injected = any(
            "## Runtime Prefetch" in str(m.get("content", "")) for m in provider.last_messages
        )
        assert injected is False
