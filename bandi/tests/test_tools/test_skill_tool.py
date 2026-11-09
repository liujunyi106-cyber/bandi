# 功能：SkillTool / SkillDiscoverTool 测试

from __future__ import annotations

import pytest

from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry
from bandi.tools import skill_tool
from bandi.tools.skill_tool import SkillDiscoverTool, SkillTool


def _make_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        SkillDef(
            name="commit",
            description="Write conventional commit messages",
            triggers=["commit", "提交"],
            instructions="Follow conventional commits.",
            allowed_tools=["apply_patch"],
            constraints=["title <= 72 chars"],
            tags=["git", "commit"],
        )
    )
    registry.register(
        SkillDef(
            name="pdf",
            description="Extract and summarize PDF documents",
            triggers=["pdf", "pdf document"],
            instructions="Extract text from the PDF.",
            tags=["document"],
        )
    )
    return registry


@pytest.fixture
def reg(monkeypatch: pytest.MonkeyPatch) -> SkillRegistry:
    registry = _make_registry()
    monkeypatch.setattr(skill_tool, "_load_registry", lambda: registry)
    return registry


class TestSkillTool:
    @pytest.mark.asyncio
    async def test_load_existing_skill(self, reg: SkillRegistry) -> None:
        tool = SkillTool()
        result = await tool.execute({"skill": "commit"})
        assert result["success"] is True
        assert result["name"] == "commit"
        assert result["instructions"] == "Follow conventional commits."
        assert result["allowed_tools"] == ["apply_patch"]
        assert result["constraints"] == ["title <= 72 chars"]

    @pytest.mark.asyncio
    async def test_load_with_leading_slash(self, reg: SkillRegistry) -> None:
        tool = SkillTool()
        result = await tool.execute({"skill": "/commit"})
        assert result["success"] is True
        assert result["name"] == "commit"

    @pytest.mark.asyncio
    async def test_load_missing_skill_lists_available(self, reg: SkillRegistry) -> None:
        tool = SkillTool()
        result = await tool.execute({"skill": "nope"})
        assert result["success"] is False
        assert "unknown skill" in result["error"]
        assert result["available"] == ["commit", "pdf"]

    @pytest.mark.asyncio
    async def test_empty_skill_name_rejected(self, reg: SkillRegistry) -> None:
        tool = SkillTool()
        result = await tool.execute({"skill": "   "})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_args_passed_through(self, reg: SkillRegistry) -> None:
        tool = SkillTool()
        result = await tool.execute({"skill": "commit", "args": "fix typo"})
        assert result["args"] == "fix typo"


class TestSkillDiscoverTool:
    @pytest.mark.asyncio
    async def test_list_all_skills(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({})
        assert result["success"] is True
        assert result["count"] == 2
        names = {s["name"] for s in result["skills"]}
        assert names == {"commit", "pdf"}

    @pytest.mark.asyncio
    async def test_filter_by_keyword(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({"keyword": "pdf"})
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "pdf"

    @pytest.mark.asyncio
    async def test_filter_no_match(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({"keyword": "zzz"})
        assert result["count"] == 0
        assert result["skills"] == []

    @pytest.mark.asyncio
    async def test_empty_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(skill_tool, "_load_registry", lambda: SkillRegistry())
        tool = SkillDiscoverTool()
        result = await tool.execute({})
        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_tags(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({"tags": ["git"]})
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "commit"

    @pytest.mark.asyncio
    async def test_filter_by_tags_multiple_any_match(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({"tags": ["git", "document"]})
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_filter_by_tags_no_match(self, reg: SkillRegistry) -> None:
        tool = SkillDiscoverTool()
        result = await tool.execute({"tags": ["missing"]})
        assert result["count"] == 0
        assert result["skills"] == []


class TestCategory:
    def test_skill_tool_category(self) -> None:
        assert SkillTool.category.value == "skills"

    def test_discover_tool_category(self) -> None:
        assert SkillDiscoverTool.category.value == "skills"
