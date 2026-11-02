# 功能：契约——领域路由低置信不裁剪工具面：仅 action=auto（≥0.85）
# 时 domain.allowed_tools 作为硬边界；confirm/clarify 档不得裁剪
# 避免单个「写」类关键词误命中内容领域导致 bash 等工具被禁。
from __future__ import annotations

import pytest

from bandi.perception.pipeline import Dependencies, PerceptionPipeline
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


class _FakeToolRegistry:
    def __init__(self, tools: list[dict]) -> None:
        self._tools = tools

    def list_flat_schemas(self) -> list[dict]:
        return self._tools


def _registry() -> _FakeToolRegistry:
    return _FakeToolRegistry(
        [
            {"name": "read", "description": "读", "parameters": {}},
            {"name": "bash", "description": "执行", "parameters": {}},
            {"name": "write", "description": "写", "parameters": {}},
            {"name": "web_search", "description": "搜索", "parameters": {}},
        ]
    )


def _surface_text(result) -> str:
    return "".join(
        m.content
        for m in result.messages
        if m.role.value == "system" and m.content.startswith("## Runtime Artifact")
    )


def _skill_registry_webonly() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        SkillDef(
            name="webonly",
            description="仅搜索",
            triggers=["调研"],
            instructions="使用搜索工具。",
            allowed_tools=["web_search"],
        )
    )
    return registry


@pytest.mark.asyncio
async def test_confirm_confidence_does_not_prune_tool_surface() -> None:
    """破坏者：单关键词命中（confirm 档）不得裁剪——「分析」1 命中 data_analysis
    （allowed=[read,bash]）时 write 必须仍在工具面。旧实现硬裁 ⇒ 红。"""
    pipeline = PerceptionPipeline(deps=Dependencies(tool_registry=_registry()))
    result = await pipeline.process("帮我分析一下这段代码")
    surface = _surface_text(result)
    assert "write" in surface, "低置信（confirm）不得裁剪工具面（write 缺失即误裁）"
    assert "bash" in surface


@pytest.mark.asyncio
async def test_auto_confidence_still_prunes_tool_surface() -> None:
    """对照组：多关键词命中（auto 档 ≥0.85）时领域仍是硬边界——
    data_analysis=[read,bash] ⇒ write 不在工具面。"""
    pipeline = PerceptionPipeline(deps=Dependencies(tool_registry=_registry()))
    result = await pipeline.process("分析数据统计趋势报告")
    surface = _surface_text(result)
    assert "write" not in surface, "高置信（auto）领域裁剪应保留"
    assert "bash" in surface


@pytest.mark.asyncio
async def test_low_confidence_skill_allowlist_not_overridden() -> None:
    """破坏者：低置信 domain × skill 交集空时不得回退 domain——
    「调研并分析一下」应保留 skill 的 web_search（用户要搜索）；
    旧实现回退 domain=[read,bash] 反而裁掉 web_search ⇒ 红。"""
    pipeline = PerceptionPipeline(
        deps=Dependencies(
            tool_registry=_registry(),
            skill_injector=SkillInjector(_skill_registry_webonly()),
        )
    )
    result = await pipeline.process("调研并分析一下")
    surface = _surface_text(result)
    assert "web_search" in surface, "低置信 domain 不得压过 skill 白名单"


@pytest.mark.asyncio
async def test_no_domain_hit_keeps_full_surface() -> None:
    """对照：无任何领域命中（clarify 档、domain=None）→ 不裁剪。"""
    pipeline = PerceptionPipeline(deps=Dependencies(tool_registry=_registry()))
    result = await pipeline.process("随便聊聊今天的天气")
    surface = _surface_text(result)
    assert "bash" in surface
    assert "write" in surface
