# 功能：PromptHarness 单元测试 — 系统提示词全量组装

from __future__ import annotations

import pytest

from bandi.agent.prompt_harness import (
    FRAMEWORK_PROMPT,
    OUTPUT_CONTROL_PROMPT,
    SAFETY_PROMPT,
    USER_EXPERIENCE_PROMPT,
    PromptHarness,
)
from bandi.ontology.lookup import EntityLookupRow
from bandi.perception.context_loader import ContextBundle


class TestPromptHarnessBuild:
    def test_build_contains_identity(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "数字员工组织的成员助手" in content

    def test_build_contains_preferred_language(self) -> None:
        harness = PromptHarness(preferred_language="en")
        content = harness.build("execute", ContextBundle())
        assert "en" in content

    def test_build_contains_framework(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert FRAMEWORK_PROMPT in content

    def test_build_contains_citation_discipline(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "不要另列「引用来源」清单" in content
        assert "chunk_id、document_id、bucket_id" in content

    def test_build_contains_safety(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert SAFETY_PROMPT in content

    def test_build_contains_output_control(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert OUTPUT_CONTROL_PROMPT in content

    def test_build_contains_ux(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert USER_EXPERIENCE_PROMPT in content

    def test_build_execute_mode(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "执行模式" in content

    def test_build_plan_mode(self) -> None:
        harness = PromptHarness()
        content = harness.build("plan", ContextBundle())
        assert "计划模式" in content

    def test_build_ask_mode_falls_back_identity(self) -> None:
        """双模式化：ask 模式段已删除，build 不再渲染问答模式提示词。"""
        harness = PromptHarness()
        content = harness.build("ask", ContextBundle())
        assert "问答模式" not in content

    def test_build_without_tools(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "可用工具" not in content

    def test_build_with_empty_tools_ignored(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "可用工具" not in content

    def test_build_with_ontology_schema(self) -> None:
        harness = PromptHarness()
        ctx = ContextBundle(ontology_schema="## 本体实体类型\n\n### Project")
        content = harness.build("execute", ctx)
        assert "本体实体类型" in content
        assert "Project" in content

    def test_build_without_ontology_schema(self) -> None:
        harness = PromptHarness()
        content = harness.build("execute", ContextBundle())
        assert "本体实体类型" not in content

    def test_build_ontology_before_mode(self) -> None:
        harness = PromptHarness()
        ctx = ContextBundle(ontology_schema="## 本体实体类型\n\n### Project")
        content = harness.build("execute", ctx)
        idx_onto = content.index("本体实体类型")
        idx_mode = content.index("执行模式")
        assert idx_onto < idx_mode

    def test_build_with_entities(self) -> None:
        harness = PromptHarness()
        entity = EntityLookupRow(id="p-001", name="Memo项目", entity_type="project")
        ctx = ContextBundle(referenced_entities=[entity])
        content = harness.build("execute", ctx)
        assert "Memo项目" in content
        assert "p-001" in content
        assert "project" in content

    @pytest.mark.parametrize(
        ("project_scoped", "identity_marker"),
        [
            (True, "数字员工组织的成员助手"),
            (False, "你是 Bandi 助理"),
        ],
    )
    def test_build_section_order(self, project_scoped: bool, identity_marker: str) -> None:
        """顺序：身份 → 框架 → 领域 → 模式 → 项目指令 → 实体 → 安全 → 输出控制 → 用户体感
         → 降级（工具段已抽离为工件）。身份两态：项目会话=成员 / 无项目=秘书
        。"""
        harness = PromptHarness()
        entity = EntityLookupRow(id="t-001", name="写测试", entity_type="task")
        ctx = ContextBundle(referenced_entities=[entity], project_scoped=project_scoped)
        content = harness.build("execute", ctx)
        idx_identity = content.index(identity_marker)
        idx_framework = content.index(FRAMEWORK_PROMPT)
        idx_mode = content.index("执行模式")
        idx_safety = content.index(SAFETY_PROMPT)
        idx_output = content.index(OUTPUT_CONTROL_PROMPT)
        idx_ux = content.index(USER_EXPERIENCE_PROMPT)
        assert idx_identity < idx_framework < idx_mode < idx_safety < idx_output < idx_ux

    def test_build_unknown_mode(self) -> None:
        harness = PromptHarness()
        content = harness.build("unknown", ContextBundle())
        assert "数字员工组织的成员助手" in content
        assert FRAMEWORK_PROMPT in content
        assert SAFETY_PROMPT in content
        assert OUTPUT_CONTROL_PROMPT in content
        assert USER_EXPERIENCE_PROMPT in content
        assert "执行模式" not in content


class TestPromptHarnessProjectBudget:
    """builder 对 project_instructions 做总预算裁剪，防止 system prompt 被撑爆。"""

    def test_build_trims_oversized_project_instructions(self) -> None:
        harness = PromptHarness()
        huge = "项目铁律" + ("x" * 2000)
        ctx = ContextBundle(project_instructions=huge)
        content = harness.build("execute", ctx, project_instructions_max_tokens=100)
        assert "## 项目惯例" in content
        assert "项目铁律" in content
        assert huge not in content, "超长指令必须被截断"
        assert "x" * 300 in content, "裁剪保留预算内前缀"
        assert "x" * 2000 not in content, "超预算尾部必须被丢弃"

    def test_build_keeps_small_project_instructions_untouched(self) -> None:
        harness = PromptHarness()
        small = "项目规则：中文回答"
        ctx = ContextBundle(project_instructions=small)
        content = harness.build("execute", ctx, project_instructions_max_tokens=4096)
        assert small in content
        assert "## 项目惯例" in content

    def test_build_without_budget_keeps_original(self) -> None:
        harness = PromptHarness()
        huge = "项目铁律" + ("x" * 2000)
        ctx = ContextBundle(project_instructions=huge)
        content = harness.build("execute", ctx)
        assert huge in content, "不传预算时不裁剪，保持向后兼容"
