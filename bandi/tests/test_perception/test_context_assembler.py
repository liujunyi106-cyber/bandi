# 功能：ContextAssembler 单元测试

from __future__ import annotations

from unittest.mock import MagicMock

from bandi.core.message import Message
from bandi.ontology.lookup import EntityLookupRow
from bandi.perception.context_assembler import ContextAssembler
from bandi.perception.context_loader import ContextBundle


class TestContextAssembler:
    def test_assemble_default(self) -> None:
        assembler = ContextAssembler()
        context = ContextBundle()
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context)

        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "你是 Bandi 数字员工组织的成员助手，当前模式: execute" in result[0]["content"]
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "hello"

    def test_assemble_with_harness(self) -> None:
        from bandi.agent.prompt_harness.builder import PromptSections

        harness = MagicMock()
        harness.build_sections.return_value = PromptSections(
            static="静态段", dynamic="动态段", tail="安全收尾段"
        )
        assembler = ContextAssembler(prompt_harness=harness)
        context = ContextBundle()
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context)

        harness.build_sections.assert_called_once()
        assert len(result) == 3
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "静态段", "首条 system 必须是静态段（缓存前缀）"
        assert result[1]["role"] == "system"
        assert result[1]["content"] == "动态段\n\n安全收尾段"
        assert result[2]["role"] == "user"
        assert result[2]["content"] == "hello"

    def test_assemble_passes_project_instructions_budget(self) -> None:
        """assemble 必须把 project_instructions_max_tokens 透传给 harness.build_sections。"""
        from bandi.agent.prompt_harness.builder import PromptSections

        harness = MagicMock()
        harness.build_sections.return_value = PromptSections(static="s", dynamic="d", tail="t")
        assembler = ContextAssembler(prompt_harness=harness)
        context = ContextBundle()
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context, project_instructions_max_tokens=512)

        assert len(result) == 3
        harness.build_sections.assert_called_once()
        assert (
            harness.build_sections.call_args.kwargs.get("project_instructions_max_tokens") == 512
        ), "预算必须透传到 harness.build_sections"

    def test_assemble_with_extra_context(self) -> None:
        assembler = ContextAssembler()
        context = ContextBundle()
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context, extra_context="额外上下文内容")

        # 功能：易变上下文置于末尾（历史之后、当前 user 之前），不进 system 前缀
        assert len(result) == 3
        assert "你是 Bandi 数字员工组织的成员助手，当前模式: execute" in result[0]["content"]
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "额外上下文内容"
        assert result[1]["meta"]["injected_context"] is True
        assert result[2]["role"] == "user"
        assert result[2]["content"] == "hello"

    def test_assemble_with_entities(self) -> None:
        assembler = ContextAssembler()
        entity = EntityLookupRow(id="e1", name="深度学习", entity_type="knowledge")
        context = ContextBundle(referenced_entities=[entity])
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context)

        assert len(result) == 2
        content = result[0]["content"]
        assert "相关实体" in content
        assert "深度学习" in content
        assert "knowledge" in content
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "hello"

    def test_default_prompt(self) -> None:
        assembler = ContextAssembler()
        context = ContextBundle()
        msg = Message(role="user", content="hello")
        result = assembler.assemble(msg, context)

        content = result[0]["content"]
        assert content == "你是 Bandi 数字员工组织的成员助手，当前模式: execute。"
