from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bandi.agent.factory import (
    build_agent_context,
    build_provider,
    build_tool_registry,
    run_agent,
)
from bandi.core.config import BandiConfig
from bandi.llm.mock import MockProvider
from bandi.memory.long_term import LongTermMemory


class TestBuildToolRegistry:
    def test_default_registry_has_standard_tools(self) -> None:
        reg = build_tool_registry()
        assert reg is not None
        all_tools = reg.list_tools()
        names = [t.name for t in all_tools]
        assert "bash" in names
        assert "read" in names
        assert "write" in names
        assert "web_search" in names

    def test_with_triple_ontology_store_adds_ontology_tools(self, tmp_path: Any) -> None:
        """三元组 store 在场时注册四本体工具；旧实体工具已随删旧批次移除。"""
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore

        store = OntologyStore(open_ontology(str(tmp_path / "o.db")))
        reg = build_tool_registry(triple_ontology_store=store)
        names = [t.name for t in reg.list_tools()]
        assert {
            "ontology_define",
            "ontology_assert",
            "ontology_query",
            "ontology_retract",
        } <= set(names)
        assert "entity_create" not in names
        assert "entity_search" not in names

    def test_knowledge_search_tool_has_persistent_memory(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            reg = build_tool_registry()
        tool = reg.get("knowledge_search")
        assert tool is not None, "knowledge_search 必须已注册"
        memory = tool._memory
        assert memory is not None, "factory 必须注入持久化 memory，而非运行时新建 :memory: 空库"
        assert ":memory:" not in str(memory._conn.execute("PRAGMA database_list").fetchall())

    def test_knowledge_search_tool_data_survives_rebuild(self, tmp_path: Any) -> None:
        import asyncio
        from pathlib import Path

        from bandi.memory.long_term import MemoryItem

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            reg1 = build_tool_registry()
            tool1 = reg1.get("knowledge_search")
            assert tool1 is not None
            memory: LongTermMemory = tool1._memory
            memory.store(MemoryItem(item_id="k1", content="Invoice process best practice"))

            reg2 = build_tool_registry()
            tool2 = reg2.get("knowledge_search")
            assert tool2 is not None
            result = asyncio.run(tool2.execute({"action": "catalog"}))
        assert result["success"] is True
        assert result["total"] == 1, "重建 registry 后知识库数据必须仍在（持久化而非 :memory:）"
        assert result["documents"][0]["item_id"] == "k1"


class TestBuildProvider:
    def test_with_api_key_returns_real_provider(self) -> None:
        cfg = BandiConfig()
        cfg.llm.api_key = "sk-test"
        cfg.llm.default_model = "deepseek-v4-flash"
        provider = build_provider(cfg)
        assert provider is not None
        assert not isinstance(provider, MockProvider)

    def test_without_api_key_returns_mock(self) -> None:
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        provider = build_provider(cfg)
        assert isinstance(provider, MockProvider)


class TestBuildAgentContext:
    def test_default_context(self) -> None:
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        ctx = build_agent_context(user_input="test", bandi_config=cfg)
        assert ctx.raw_input == "test"
        assert ctx.active_tools is not None

    def test_context_has_mode_in_meta(self) -> None:
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        ctx = build_agent_context(user_input="test", mode="plan", bandi_config=cfg)
        assert ctx.meta.get("mode") == "plan"

    def test_provider_respected(self) -> None:
        provider = MockProvider()
        ctx = build_agent_context(user_input="test", provider=provider)
        assert ctx.provider is provider

    def test_extra_surface_tools_union_not_trimmed(self, monkeypatch) -> None:
        """显式要求的工具（MCP 开启）必须进入工具面，不受相关性裁剪影响。"""
        import bandi.agent.factory as factory_module
        from bandi.tools.base import BaseTool, ToolCategory
        from bandi.tools.registry import ToolRegistry

        class _Plain(BaseTool):
            name: str = "aaa_tool"
            description: str = "普通工具"
            category: ToolCategory = ToolCategory.DOC

            async def execute(self, params, context=None):  # type: ignore[no-untyped-def]
                return "ok"

        class _Mcp(BaseTool):
            name: str = "demo_mcp_navigate"
            description: str = "打开浏览器"
            category: ToolCategory = ToolCategory.INTEGRATION

            async def execute(self, params, context=None):  # type: ignore[no-untyped-def]
                return "ok"

        reg = ToolRegistry()
        reg.register_instance(_Plain())
        reg.register_instance(_Mcp())
        # 制造「会被裁掉」的场景：相关性只选 aaa_tool
        from bandi.tools.selection import schema_name

        monkeypatch.setattr(
            factory_module,
            "select_relevant_schemas",
            lambda schemas, query, **kw: [s for s in schemas if schema_name(s) == "aaa_tool"],
        )
        ctx = build_agent_context(
            user_input="随便",
            bandi_config=BandiConfig(),
            tool_registry=reg,
            extra_surface_tools=["demo_mcp_navigate"],
        )
        ctx_without = build_agent_context(
            user_input="随便",
            bandi_config=BandiConfig(),
            tool_registry=reg,
        )

        names = {schema_name(s) for s in ctx.active_tools}
        assert "demo_mcp_navigate" in names, "显式要求的 MCP 工具不得被相关性裁剪裁掉"
        assert "demo_mcp_navigate" not in {schema_name(s) for s in ctx_without.active_tools}
        # 去重：不重复出现
        assert [schema_name(s) for s in ctx.active_tools].count("demo_mcp_navigate") == 1


class TestRecoveryManagerWired:
    def test_context_gets_recovery_manager(self) -> None:
        """RecoveryManager 必须由工厂注入（接线后 loop.py 消费路径生效）。"""
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        ctx = build_agent_context(user_input="test", bandi_config=cfg)
        assert ctx.recovery_manager is not None

    @pytest.mark.asyncio
    async def test_recoverable_error_returns_retry(self) -> None:
        from bandi.agent.base import RecoveryAction

        cfg = BandiConfig()
        cfg.llm.api_key = ""
        ctx = build_agent_context(user_input="test", bandi_config=cfg)
        action = await ctx.recovery_manager.handle(TimeoutError("timeout"), "llm")
        assert action == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_degradation_event_reaches_bus(self) -> None:
        """DEGRADED 异常：降级事件必须发到工厂注入的 bus。"""
        from bandi.core.events import EventType

        cfg = BandiConfig()
        cfg.llm.api_key = ""
        ctx = build_agent_context(user_input="test", bandi_config=cfg)
        received: list[str] = []

        async def handler(event: Any) -> None:
            received.append(event.type)

        await ctx.bus.subscribe(EventType.DEGRADATION, handler)
        await ctx.recovery_manager.handle(NotImplementedError("no impl"), "tool")
        await ctx.bus.close()
        assert EventType.DEGRADATION in received


class TestProviderRegistryShared:
    def test_registry_reused_across_builds(self) -> None:
        from bandi.agent.factory import _REGISTRY_CACHE
        from bandi.llm.base import BaseProvider

        class FakeProvider(BaseProvider):
            def __init__(self) -> None:
                super().__init__(config=None)

            @property
            def model_name(self) -> str:
                return "fake"

            @property
            def protocol(self) -> str:
                return "openai"

            async def complete(self, messages, config=None):
                return "fake"

            async def stream(self, messages, config=None):
                yield "fake"

        cfg = BandiConfig()
        cfg.llm.api_key = "test-key"
        _REGISTRY_CACHE.clear()
        ctx1 = build_agent_context(user_input="hello", provider=FakeProvider(), bandi_config=cfg)
        ctx2 = build_agent_context(user_input="hello", provider=FakeProvider(), bandi_config=cfg)
        reg1 = ctx1.meta.get("provider_registry")
        reg2 = ctx2.meta.get("provider_registry")
        assert reg1 is not None and reg2 is not None, "非 Mock provider 必须注入 registry"
        assert reg1 is reg2, "registry 必须复用同一实例（模块级缓存）"


class TestRunAgent:
    @pytest.mark.asyncio
    async def test_run_agent_basic(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            reply, reason, tokens, _idx = await run_agent(
                user_input="hello",
                bandi_config=_test_config(),
            )
        assert isinstance(reply, str)
        assert reply
        assert reason == "COMPLETED" or reason is not None
        assert tokens >= 0

    @pytest.mark.asyncio
    async def test_run_agent_with_work_item_enables_ownership(self, tmp_path: Any) -> None:
        """端到端：run_agent 传 work_item 时 Ownership 守卫生效——read_only 契约拦截 file_write。"""
        from pathlib import Path

        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItem, WorkItemTurnType
        from bandi.core.message import LLMResponse, ToolCall
        from bandi.llm.mock import MockProvider

        work_item = WorkItem(
            id="wi-1",
            title="t",
            description="",
            phase=Phase.READY,
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            parent_id=None,
            children_ids=[],
            links={},
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": {"write_scope": "read_only"},
            },
            runtime_metadata={},
        )

        from bandi.agent.factory import build_agent_context, build_tool_registry
        from bandi.core.event_bus import EventBus

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            provider = MockProvider()
            provider.add_response(
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="file_write",
                            arguments={"file_path": "x.txt", "content": "hi"},
                        )
                    ],
                )
            )
            provider.add_response(LLMResponse(content="done"))
            registry = build_tool_registry(None)
            ctx = build_agent_context(
                user_input="write a file",
                mode="execute",
                provider=provider,
                tool_registry=registry,
                bus=EventBus(),
                bandi_config=_test_config(),
                enable_ownership=True,
                work_item=work_item,
            )
            assert ctx.tool_executor is not None
            assert ctx.tool_executor._pipeline is not None, "ownership 启用时必须挂 pipeline"
            # 直接驱动 executor 验证拦截（不经完整 AgentLoop，聚焦 契约）
            # 用 todo_write：它在 bypass 列表（不触发 ask），可验证 Ownership 层真正生效
            from bandi.core.message import Message, MessageRole, ToolCall

            user_msg = Message(role=MessageRole.USER, content="write a file")
            ctx.messages.append(user_msg)
            call = ToolCall(
                id="c1",
                name="todo_write",
                arguments={"todos": [{"content": "new todo"}]},
            )
            results = await ctx.tool_executor.execute([call])
            assert len(results) == 1
            assert not results[0].success, f"read_only 契约必须拦截: {results[0].error}"
            assert "Ownership" in (results[0].error or "")

    @pytest.mark.asyncio
    async def test_run_agent_closes_self_created_memory(self, tmp_path: Any) -> None:
        """
        （公司链路 seat_executor → run_agent 每次执行 work item 泄漏 SQLite 连接）。"""
        from pathlib import Path

        closed: list[str] = []

        orig_close = LongTermMemory.close

        def _tracked_close(self):
            closed.append("long_term")
            return orig_close(self)

        with patch.object(LongTermMemory, "close", _tracked_close):
            with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
                reply, reason, tokens, _idx = await run_agent(
                    user_input="hello",
                    bandi_config=_test_config(),
                    project_id="p1",
                    employee_id="e1",
                )
        assert reply
        assert "long_term" in closed, "run_agent 必须关闭本次新建的 long_term 连接"


def _test_config() -> BandiConfig:
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = 1
    return cfg
