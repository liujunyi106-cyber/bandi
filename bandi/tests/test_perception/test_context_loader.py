from typing import Any

from bandi.core.message import Message
from bandi.memory.session.store import SessionStore
from bandi.perception.context_loader import ContextBundle, ContextLoader


class TestContextBundle:
    def test_ontology_schema_default_empty(self) -> None:
        bundle = ContextBundle()
        assert bundle.ontology_schema == ""

    def test_ontology_schema_can_be_set(self) -> None:
        bundle = ContextBundle(ontology_schema="## 本体实体类型")
        assert bundle.ontology_schema == "## 本体实体类型"


class TestContextLoader:
    def test_ontology_schema_empty_without_store(self) -> None:
        """无本体存储时段落为空——本体是增强，不是主链依赖。"""
        store = SessionStore(":memory:")
        loader = ContextLoader(store)
        assert loader._ontology_schema == ""

    def test_load_passes_ontology_schema(self, tmp_path: Any) -> None:
        """注入三元组存储后段落渲染词表；惰性重渲（构造期不缓存）。"""
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore

        ontology = OntologyStore(open_ontology(str(tmp_path / "o.db")))
        ontology.define_class({"id": "Project", "label": "项目"}, "2026-01-01T00:00:00")
        store = SessionStore(":memory:")
        loader = ContextLoader(store, ontology_store=ontology)
        assert loader._ontology_schema.startswith("# 本体")
        msg = Message(role="user", content="hello")
        import asyncio

        bundle = asyncio.run(loader.load(msg, ""))
        assert "本体" in bundle.ontology_schema


class TestContextLoaderToolCallsRestore:
    def _seed_with_tool_calls(self, store: SessionStore, sid: str) -> None:
        from bandi.core.message import MessageRole, ToolCall

        store.append_message(
            sid,
            Message(
                role=MessageRole.ASSISTANT,
                content="调用工具",
                tool_calls=[ToolCall(id="tc1", name="read", arguments={"path": "a.txt"})],
            ),
        )

    def test_load_restores_tool_calls(self) -> None:
        import asyncio

        from bandi.core.message import MessageRole

        store = SessionStore(":memory:")
        sid = store.create_session()
        self._seed_with_tool_calls(store, sid)
        loader = ContextLoader(store)
        msg = Message(role="user", content="继续")
        bundle = asyncio.run(loader.load(msg, sid))
        assert len(bundle.history) == 1
        restored = bundle.history[0]
        assert restored.role in ("assistant", MessageRole.ASSISTANT)
        assert restored.tool_calls, "读回消息必须还原 tool_calls"
        assert restored.tool_calls[0].name == "read"
        assert restored.tool_calls[0].arguments == {"path": "a.txt"}

    def test_load_restores_message_without_tool_calls_untouched(self) -> None:
        import asyncio

        from bandi.core.message import MessageRole

        store = SessionStore(":memory:")
        sid = store.create_session()
        store.append_message(
            sid,
            Message(role=MessageRole.USER, content="普通问题"),
        )
        loader = ContextLoader(store)
        msg = Message(role="user", content="继续")
        bundle = asyncio.run(loader.load(msg, sid))
        assert len(bundle.history) == 1
        assert bundle.history[0].tool_calls in (None, [])

    def test_message_from_entry_normalizes_str_arguments(self) -> None:
        """深度复审修复：metadata 中 arguments 为 JSON 字符串时必须归一为 dict"""
        from bandi.core.message import message_from_entry

        msg = message_from_entry(
            {
                "role": "assistant",
                "content": "调用",
                "metadata": {
                    "tool_calls": [{"id": "tc1", "name": "bash", "arguments": '{"cmd": "ls"}'}]
                },
            }
        )
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"cmd": "ls"}, (
            f"str arguments 必须解析为 dict，实际 {msg.tool_calls[0].arguments!r}"
        )

    def test_message_from_entry_invalid_str_arguments_defaults_empty(self) -> None:
        """深度复审修复：非法 JSON 字符串 arguments 降级为空 dict（不崩溃）。"""
        from bandi.core.message import message_from_entry

        msg = message_from_entry(
            {
                "role": "assistant",
                "content": "x",
                "metadata": {"tool_calls": [{"id": "t", "name": "n", "arguments": "not-json{"}]},
            }
        )
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_load_restores_after_reopen(self) -> None:
        """跨进程/新 store 连接读取同一库，tool_calls 仍可还原（DB 持久化断言）。"""
        import asyncio
        import tempfile
        from pathlib import Path

        from bandi.core.message import MessageRole, ToolCall

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "s.db")
            store1 = SessionStore(db)
            sid = store1.create_session()
            store1.append_message(
                sid,
                Message(
                    role=MessageRole.ASSISTANT,
                    content="x",
                    tool_calls=[ToolCall(id="tc2", name="grep", arguments={})],
                ),
            )
            store1.close()
            store2 = SessionStore(db)
            try:
                loader = ContextLoader(store2)
                msg = Message(role="user", content="继续")
                bundle = asyncio.run(loader.load(msg, sid))
                assert bundle.history[0].tool_calls[0].name == "grep"
            finally:
                store2.close()


class TestContextLoaderBudget:
    """max_tokens 透传 + estimated_tokens 纳入 project_instructions。"""

    def test_load_passes_max_tokens_to_memory_context(self, tmp_path) -> None:
        from unittest.mock import Mock

        store = SessionStore(":memory:")
        hierarchy = Mock()
        hierarchy.collect_memory_context.return_value = "记忆上下文"
        loader = ContextLoader(
            store,
            project_root=str(tmp_path),
            hierarchy=hierarchy,
            project_id="p1",
            employee_id="e1",
        )
        msg = Message(role="user", content="hello")
        import asyncio

        bundle = asyncio.run(loader.load(msg, "s1", max_tokens=1234))
        hierarchy.collect_memory_context.assert_called_once_with(
            project_id="p1",
            session_id="s1",
            employee_id="e1",
            group_conversation_id="",
            dm_conversation_id="",
            max_tokens=1234,
            team_id=None,
        )
        assert "记忆上下文" in bundle.memory_context
        assert bundle.project_instructions == ""

    def test_load_passes_team_id_to_memory_context(self, tmp_path) -> None:
        """契约：team_id 全链透传——构造入参必须原样到达
        collect_memory_context（否则注入过滤永远拿不到团队上下文）。"""
        from unittest.mock import Mock

        store = SessionStore(":memory:")
        hierarchy = Mock()
        hierarchy.collect_memory_context.return_value = "记忆上下文"
        loader = ContextLoader(
            store,
            project_root=str(tmp_path),
            hierarchy=hierarchy,
            project_id="p1",
            employee_id="e1",
            team_id="t9",
        )
        msg = Message(role="user", content="hello")
        import asyncio

        asyncio.run(loader.load(msg, "s1"))
        hierarchy.collect_memory_context.assert_called_once_with(
            project_id="p1",
            session_id="s1",
            employee_id="e1",
            group_conversation_id="",
            dm_conversation_id="",
            max_tokens=4096,
            team_id="t9",
        )

    def test_estimated_tokens_includes_project_instructions(self, tmp_path) -> None:
        store = SessionStore(":memory:")
        loader = ContextLoader(store)
        msg = Message(role="user", content="hello world")
        import asyncio

        bundle = asyncio.run(loader.load(msg, ""))
        from bandi.utils.token_counter import TokenCounter

        assert bundle.estimated_tokens >= TokenCounter.estimate("hello world")
        if bundle.project_instructions:
            assert bundle.estimated_tokens >= TokenCounter.estimate(bundle.project_instructions)

    def test_estimated_tokens_includes_loaded_project_instructions(self, tmp_path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "AGENTS.md").write_text("项目规则：" + "铁律" * 100, encoding="utf-8")
        store = SessionStore(":memory:")
        loader = ContextLoader(store, project_root=str(proj))
        msg = Message(role="user", content="hi")
        import asyncio

        bundle = asyncio.run(loader.load(msg, ""))
        from bandi.utils.token_counter import TokenCounter

        assert bundle.project_instructions, "AGENTS.md 必须被加载为 project_instructions"
        assert bundle.estimated_tokens >= TokenCounter.estimate(bundle.project_instructions), (
            "estimated_tokens 必须纳入 project_instructions 的 token 数"
        )
