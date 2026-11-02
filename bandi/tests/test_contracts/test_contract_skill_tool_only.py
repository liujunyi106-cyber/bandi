# 功能：技能 F 模式契约——技能正文只在模型调用 skill_invoke 时进入上下文；
# 目录常驻、绑定只影响可见域与目录排序，不因触发词或档案绑定自动注入正文。
from __future__ import annotations

import pytest

from bandi.core.events import EventType
from bandi.core.message import ToolCall
from bandi.perception.pipeline import Dependencies, PerceptionPipeline
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry
from bandi.tools.executor import ToolExecutor

_BODY = "SECRET_BODY：先反问澄清，再产出视觉 DNA 卡。"


class _Registry:
    def __init__(self, tools: list[object]) -> None:
        self._tools = {getattr(tool, "name", ""): tool for tool in tools}

    def get(self, name: str) -> object | None:
        return self._tools.get(name)


class _FakeSkillInvoke:
    name = "skill_invoke"
    description = "fake"
    risk_level = "low"
    is_concurrency_safe = True
    is_idempotent = True
    is_read_only = True
    tags: list[str] = []
    cache_ttl = 0.0
    enabled = True
    permissions_override = None
    permission_action = ""
    mcp_remote = False
    parameters: dict[str, object] = {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, object], **kwargs: object) -> dict[str, object]:
        return {"success": True, "name": "demo-skill", "instructions": "正文"}

    def validate_params(self, params: dict[str, object]) -> list[str]:
        return []

    def to_openai_schema(self) -> dict[str, object]:
        return {}


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def _registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        SkillDef(
            name="visual-clarify",
            description="视觉设计师的澄清式反问工作流",
            triggers=["高级感", "视觉风格"],
            instructions=_BODY,
        )
    )
    registry.register(
        SkillDef(
            name="finesse-ui",
            description="高质量 Web 界面构建方法",
            triggers=[],
            instructions="SECRET_BODY：品牌/产品/电商三档设计流程。",
        )
    )
    return registry


class TestSkillToolOnlyMode:
    def test_trigger_word_does_not_inject_body(self) -> None:
        catalog = SkillInjector(_registry()).build_catalog()
        assert "visual-clarify" in catalog
        assert _BODY not in catalog

    def test_bound_skill_does_not_inject_body(self) -> None:
        catalog = SkillInjector(
            _registry(),
            bound_names=["visual-clarify", "finesse-ui"],
        ).build_catalog()
        assert "visual-clarify" in catalog
        assert "岗位推荐" in catalog
        assert _BODY not in catalog

    def test_catalog_still_lists_skill_names(self) -> None:
        catalog = SkillInjector(_registry()).build_catalog()
        assert "## 可用技能" in catalog
        assert "visual-clarify" in catalog
        assert "finesse-ui" in catalog
        assert "skill_invoke" in catalog

    @pytest.mark.asyncio
    async def test_pipeline_prompt_has_catalog_but_no_skill_body(self) -> None:
        pipeline = PerceptionPipeline(deps=Dependencies(skill_injector=SkillInjector(_registry())))
        result = await pipeline.process("这个页面高级感怎么提升")
        text = "\n".join(message.content or "" for message in result.messages)
        assert "## 可用技能" in text
        assert "visual-clarify" in text
        assert "SECRET_BODY" not in text
        assert "## Skill 指令" not in text

    @pytest.mark.asyncio
    async def test_skill_invoke_emits_activation_event(self) -> None:
        bus = _Bus()
        executor = ToolExecutor(_Registry([_FakeSkillInvoke()]), event_bus=bus)
        results = await executor.execute(
            [ToolCall(id="1", name="skill_invoke", arguments={"skill": "demo-skill"})]
        )
        assert results[0].success
        emitted = [
            list(getattr(event, "data", {}).get("skills", []))
            for event in bus.events
            if getattr(event, "type", None) == EventType.SKILL_INJECTED
        ]
        assert emitted == [["demo-skill"]], "skill_invoke 成功必须上报技能激活事件"
