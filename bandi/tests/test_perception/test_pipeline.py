# 功能：Perception 层单元测试 — IntentRouter / ContextAssembler / Pipeline

import json
import os
import tempfile

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore
from bandi.perception.context_assembler import ContextAssembler
from bandi.perception.context_loader import ContextBundle
from bandi.perception.intent_router import IntentRouter
from bandi.perception.pipeline import Dependencies, PerceptionPipeline
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def skill_registry_with_webonly() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        SkillDef(
            name="webonly",
            description="仅搜索",
            triggers=["搜索", "调研"],
            instructions="使用搜索工具。",
            allowed_tools=["web_search"],
        )
    )
    return registry


class TestIntentRouter:
    def test_execute_default(self) -> None:
        router = IntentRouter()
        msg = Message(role=MessageRole.USER, content="创建新项目")
        assert router.route(msg) == "execute"

    def test_ask_mode(self) -> None:
        router = IntentRouter()
        msg = Message(role=MessageRole.USER, content="什么是 ontology？")
        assert router.route(msg) == "execute"

    def test_plan_mode(self) -> None:
        router = IntentRouter()
        msg = Message(role=MessageRole.USER, content="计划明天的工作")
        assert router.route(msg) == "plan"

    def test_ask_keyword_en(self) -> None:
        router = IntentRouter()
        msg = Message(role=MessageRole.USER, content="how to build a memo")
        assert router.route(msg) == "execute"

    def test_plan_keyword_en(self) -> None:
        router = IntentRouter()
        msg = Message(role=MessageRole.USER, content="plan the architecture")
        assert router.route(msg) == "plan"


class TestContextAssembler:
    def test_assemble_basic(self) -> None:
        assembler = ContextAssembler()
        msg = Message(role=MessageRole.USER, content="hello")
        ctx = ContextBundle()
        messages = assembler.assemble(msg, ctx, mode="execute")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"

    def test_assemble_with_history(self) -> None:
        assembler = ContextAssembler()
        msg = Message(role=MessageRole.USER, content="第二轮")
        ctx = ContextBundle(history=[Message(role=MessageRole.USER, content="第一轮")])
        messages = assembler.assemble(msg, ctx)
        assert len(messages) == 3
        assert messages[1]["content"] == "第一轮"

    def test_system_prompt_structure(self) -> None:
        assembler = ContextAssembler()
        msg = Message(role=MessageRole.USER, content="hello")
        ctx = ContextBundle()
        messages = assembler.assemble(msg, ctx, mode="execute")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "execute" in messages[0]["content"]

    def test_plan_mode_prompt(self) -> None:
        assembler = ContextAssembler()
        msg = Message(role=MessageRole.USER, content="hello")
        ctx = ContextBundle()
        messages = assembler.assemble(msg, ctx, mode="plan")
        assert "plan" in messages[0]["content"]


class TestPerceptionPipeline:
    @pytest.mark.asyncio
    async def test_process_basic(self) -> None:
        pipeline = PerceptionPipeline()
        result = await pipeline.process("你好")
        assert result.mode == "execute"
        assert len(result.messages) == 2
        assert result.messages[0].role.value == "system"
        assert result.messages[-1].role.value == "user"

    @pytest.mark.asyncio
    async def test_process_with_session(self) -> None:
        tmp = tempfile.mktemp(suffix=".db")
        store = SessionStore(tmp)
        from bandi.core.message import Message, MessageRole

        sid = store.create_session(title="测试")
        store.append_message(sid, Message(role=MessageRole.USER, content="第一轮"))
        pipeline = PerceptionPipeline(deps=Dependencies(session_store=store))
        result = await pipeline.process("第二轮", session_id=sid)
        assert len(result.messages) == 3  # system + history + user
        store.close()
        import os

        os.unlink(tmp)
        tmp = tempfile.mktemp(suffix=".db")
        store = SessionStore(tmp)
        from bandi.core.message import Message, MessageRole

        sid = store.create_session(title="测试")
        store.append_message(sid, Message(role=MessageRole.USER, content="第一轮"))
        pipeline = PerceptionPipeline(deps=Dependencies(session_store=store))
        result = await pipeline.process("第二轮", session_id=sid)
        assert len(result.messages) == 3  # system + history + user
        store.close()
        import os

        os.unlink(tmp)

    @pytest.mark.asyncio
    async def test_mode_ask(self) -> None:
        pipeline = PerceptionPipeline()
        result = await pipeline.process("什么是 ontology？")
        assert result.mode == "execute", "双模式化：问句不再切 ask，session_mode 兜底 execute"

    @pytest.mark.asyncio
    async def test_mode_plan(self) -> None:
        pipeline = PerceptionPipeline()
        result = await pipeline.process("计划开发路线图")
        assert result.mode == "execute"
        assert result.intent_hint == "plan"

    @pytest.mark.asyncio
    async def test_entities_referenced(self) -> None:
        # 无实体图注入，@xxxx 引用不会被解析
        pipeline = PerceptionPipeline()
        result = await pipeline.process("查看@p-001状态")
        assert result.entities_referenced == []

    @pytest.mark.asyncio
    async def test_estimated_tokens(self) -> None:
        pipeline = PerceptionPipeline()
        result = await pipeline.process("你好")
        assert result.estimated_input_tokens > 0

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        pipeline = PerceptionPipeline()
        result = await pipeline.process("")
        assert len(result.messages) == 0
        assert result.mode == "execute"

    @pytest.mark.asyncio
    async def test_collect_all_entity_items_deduplicated(
        self,
    ) -> None:
        """
        必须过滤（否则 referenced_entities + extra_context 双注入 system prompt）。"""
        from bandi.context.item import ContextItem
        from bandi.context.source import ContextSourceRegistry
        from bandi.perception.pipeline import Dependencies

        class _EntityRegistrySource:
            async def collect(
                self,
                message: Message,
                session_id: str,
                budget: int = 4096,
                intent_hint: str | None = None,
            ) -> list[ContextItem]:
                return [
                    ContextItem(
                        id="entity:p-001",
                        source_type="entity",
                        content="实体：恒星贸易项目(project)",
                        priority=3,
                    )
                ]

        class _LoaderWithEntities:
            async def load(
                self, message: Message, session_id: str, max_tokens: int = 4096
            ) -> ContextBundle:
                from bandi.ontology.lookup import EntityLookupRow

                entity = EntityLookupRow(
                    id="p-001",
                    name="恒星贸易项目",
                    entity_type="project",
                )
                return ContextBundle(referenced_entities=[entity])

        registry = ContextSourceRegistry()
        registry.register("entity", _EntityRegistrySource())
        pipeline = PerceptionPipeline(deps=Dependencies(session_store=None))
        pipeline.loader = _LoaderWithEntities()
        pipeline._context_registry = registry
        result = await pipeline.process("查看@恒星贸易项目状态", session_id="s-1")
        system_text = "".join(m.content for m in result.messages if m.role.value == "system")
        assert "相关实体" in system_text, "实体必须经 loader 权威路径注入"
        assert system_text.count("恒星贸易项目") == 1, (
            "实体不得双注入（loader referenced_entities + collect_all entity 各一份）"
        )


class _FakeToolRegistry:
    def __init__(self, tools: list[dict]) -> None:
        self._tools = tools

    def list_flat_schemas(self) -> list[dict]:
        return self._tools


class TestContextAssemblerToolSurface:
    def test_allowed_none_shows_all_tools(self) -> None:
        assembler = ContextAssembler(
            tool_registry=_FakeToolRegistry(
                [
                    {"name": "read", "description": "读", "parameters": {}},
                    {"name": "bash", "description": "执行", "parameters": {}},
                ]
            )
        )
        artifacts = assembler.collect_runtime_artifacts(allowed_tools=None)
        assert len(artifacts) == 1
        assert "read" in artifacts[0].content and "bash" in artifacts[0].content

    def test_allowed_non_empty_filters(self) -> None:
        assembler = ContextAssembler(
            tool_registry=_FakeToolRegistry(
                [
                    {"name": "read", "description": "读", "parameters": {}},
                    {"name": "bash", "description": "执行", "parameters": {}},
                ]
            )
        )
        artifacts = assembler.collect_runtime_artifacts(allowed_tools=["read"])
        assert len(artifacts) == 1
        assert "read" in artifacts[0].content
        assert "bash" not in artifacts[0].content

    def test_allowed_empty_means_no_restriction(self) -> None:
        """空列表 = 不限制工具（Day122v e0b91b8 修复语义；空列表不能禁止全部工具）。"""
        assembler = ContextAssembler(
            tool_registry=_FakeToolRegistry(
                [
                    {"name": "read", "description": "读", "parameters": {}},
                    {"name": "bash", "description": "执行", "parameters": {}},
                ]
            )
        )
        artifacts = assembler.collect_runtime_artifacts(allowed_tools=[])
        assert len(artifacts) == 1
        assert "read" in artifacts[0].content and "bash" in artifacts[0].content


class TestDomainSkillToolMerge:
    @pytest.mark.asyncio
    async def test_domain_skill_disjoint_falls_back_to_domain(self) -> None:
        """#15：domain×skill 工具交集为空时回退 domain_allowed（domain 为硬边界）。"""
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.pipeline import Dependencies

        tool_registry = _FakeToolRegistry(
            [
                {"name": "read", "description": "读", "parameters": {}},
                {"name": "bash", "description": "执行", "parameters": {}},
                {"name": "web_search", "description": "搜索", "parameters": {}},
            ]
        )
        pipeline = PerceptionPipeline(
            deps=Dependencies(
                tool_registry=tool_registry,
                prompt_harness=PromptHarness(),
                skill_injector=SkillInjector(skill_registry_with_webonly()),
            )
        )
        result = await pipeline.process("帮我搜索排期项目进度")
        tool_surface = "".join(
            m.content
            for m in result.messages
            if m.role.value == "system" and m.content.startswith("## Runtime Artifact")
        )
        assert "bash" in tool_surface
        assert "web_search" not in tool_surface


class TestSessionArtifactHashPersistence:
    @pytest.mark.asyncio
    async def test_hashes_persisted_to_session_metadata(self) -> None:
        tmp = tempfile.mktemp(suffix=".db")
        store = SessionStore(tmp)
        sid = store.create_session(title="测试")
        registry = _FakeToolRegistry([{"name": "search", "description": "搜索", "parameters": {}}])
        pipeline = PerceptionPipeline(
            deps=Dependencies(session_store=store, tool_registry=registry)
        )
        await pipeline.process("第一轮", session_id=sid)
        session = store.get_session(sid)
        metadata = json.loads(session["metadata"])
        assert "artifact_hashes" in metadata
        assert isinstance(metadata["artifact_hashes"], dict)
        store.close()
        os.unlink(tmp)

    @pytest.mark.asyncio
    async def test_second_pipeline_instance_reuses_hashes(self) -> None:
        tmp = tempfile.mktemp(suffix=".db")
        store = SessionStore(tmp)
        sid = store.create_session(title="测试")
        registry = _FakeToolRegistry([{"name": "search", "description": "搜索", "parameters": {}}])
        first = PerceptionPipeline(deps=Dependencies(session_store=store, tool_registry=registry))
        await first.process("第一轮", session_id=sid)
        second = PerceptionPipeline(deps=Dependencies(session_store=store, tool_registry=registry))
        result = await second.process("第二轮", session_id=sid)
        # 同工具集第二轮不再产出任何 Runtime Artifact 消息
        artifact_msgs = [
            m for m in result.messages if (m.content or "").startswith("## Runtime Artifact")
        ]
        assert artifact_msgs == []
        store.close()
        os.unlink(tmp)

    @pytest.mark.asyncio
    async def test_new_tool_triggers_delta_across_instances(self) -> None:
        tmp = tempfile.mktemp(suffix=".db")
        store = SessionStore(tmp)
        sid = store.create_session(title="测试")
        registry = _FakeToolRegistry([{"name": "search", "description": "搜索", "parameters": {}}])
        first = PerceptionPipeline(deps=Dependencies(session_store=store, tool_registry=registry))
        await first.process("第一轮", session_id=sid)
        registry2 = _FakeToolRegistry(
            [
                {"name": "search", "description": "搜索", "parameters": {}},
                {"name": "write", "description": "写文件", "parameters": {}},
            ]
        )
        second = PerceptionPipeline(deps=Dependencies(session_store=store, tool_registry=registry2))
        result = await second.process("第二轮", session_id=sid)
        assert result.messages[1].role.value == "system"
        assert "Delta" in result.messages[1].content
        assert "write" in result.messages[1].content
        store.close()
        os.unlink(tmp)
