from __future__ import annotations

import pytest

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.perception.context_loader import ContextBundle
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


class TestPromptHarnessSkillInjection:
    def test_prompt_with_skill_instructions(self):
        harness = PromptHarness()
        bundle = ContextBundle()
        result = harness.build("execute", bundle)
        assert "你是一个代码审查专家。" not in result
        assert "Skill 指令" not in result

    def test_prompt_with_skill_constraints(self):
        harness = PromptHarness()
        bundle = ContextBundle()
        result = harness.build("execute", bundle)
        assert "不要修改代码" not in result
        assert "Skill 约束" not in result

    def test_prompt_without_skill_params(self):
        harness = PromptHarness()
        bundle = ContextBundle()
        result = harness.build("execute", bundle)
        assert "Skill 指令" not in result
        assert "Skill 约束" not in result


class TestFullIntegration:
    @pytest.mark.asyncio
    async def test_perception_pipeline_with_skill_injector(self):
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.pipeline import Dependencies, PerceptionPipeline

        registry = SkillRegistry()
        registry.register(
            SkillDef(
                name="test-skill",
                description="测试 Skill",
                triggers=["测试"],
                instructions="你是一个测试专家。",
                constraints=["不要执行写操作"],
                allowed_tools=["read_file"],
            )
        )
        injector = SkillInjector(registry)
        pipeline = PerceptionPipeline(
            deps=Dependencies(
                prompt_harness=PromptHarness(),
                skill_injector=injector,
            )
        )
        result = await pipeline.process("帮我测试一下这段代码")
        assert result.mode == "execute"
        assert len(result.messages) > 0
        system_msg = result.messages[0]
        assert system_msg.role.value == "system"
        all_system = "".join(m.content for m in result.messages if m.role.value == "system")
        assert "test-skill" in all_system, "技能必须进目录"
        assert "你是一个测试专家。" not in all_system, "F 模式不注入正文"
        assert "不要执行写操作" not in all_system, "F 模式不注入约束"
        assert "Skill 指令" not in all_system

    @pytest.mark.asyncio
    async def test_perception_pipeline_no_skill_match(self):
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.pipeline import Dependencies, PerceptionPipeline

        registry = SkillRegistry()
        registry.register(
            SkillDef(
                name="test-skill",
                description="测试 Skill",
                triggers=["测试"],
                instructions="你是一个测试专家。",
            )
        )
        injector = SkillInjector(registry)
        pipeline = PerceptionPipeline(
            deps=Dependencies(
                prompt_harness=PromptHarness(),
                skill_injector=injector,
            )
        )
        result = await pipeline.process("什么是 Bandi")
        assert result.mode == "execute"
        system_content = result.messages[0].content
        assert "Skill 指令" not in system_content

    @pytest.mark.asyncio
    async def test_perception_pipeline_no_injector(self):
        from bandi.agent.prompt_harness.builder import PromptHarness
        from bandi.perception.pipeline import Dependencies, PerceptionPipeline

        pipeline = PerceptionPipeline(
            deps=Dependencies(prompt_harness=PromptHarness()),
        )
        result = await pipeline.process("什么是 Bandi")
        assert result.mode == "execute"
        assert "Skill 指令" not in result.messages[0].content
