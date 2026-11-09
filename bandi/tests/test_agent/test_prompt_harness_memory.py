from __future__ import annotations

from bandi.agent.prompt_harness import PromptHarness
from bandi.perception.context_loader import ContextBundle


class TestProjectMemoryRules:
    def test_memory_rules_injected_when_memory_active(self) -> None:
        harness = PromptHarness()
        ctx = ContextBundle(
            project_instructions="## 项目记忆\n\n用户偏好中文", memory_rules_active=True
        )
        content = harness.build("execute", ctx)
        assert "## 项目记忆规则" in content
        assert "MEMORY.md" in content
        assert "自动沉淀" in content

    def test_memory_rules_not_injected_without_memory_active(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "## 项目记忆规则" not in content

    def test_memory_rules_injected_even_without_project_instructions(self) -> None:
        harness = PromptHarness()
        ctx = ContextBundle(memory_rules_active=True)
        content = harness.build("execute", ctx)
        assert "## 项目记忆规则" in content

    def test_memory_rules_section_order_after_project_instructions(self) -> None:
        harness = PromptHarness()
        ctx = ContextBundle(project_instructions="## 项目记忆\n\n事实", memory_rules_active=True)
        content = harness.build("execute", ctx)
        idx_rules = content.index("## 项目记忆规则")
        idx_entities = (
            content.index("## 当前上下文实体") if "## 当前上下文实体" in content else len(content)
        )
        assert idx_rules < idx_entities or idx_entities == len(content)
