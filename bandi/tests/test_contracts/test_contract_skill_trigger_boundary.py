# 功能：契约测试——技能触发词按词边界匹配（ASCII），中文按子串匹配
from __future__ import annotations

from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(name: str, triggers: list[str]) -> SkillDef:
    return SkillDef(name=name, description="d", triggers=triggers, instructions="i")


class TestMatchTriggerBoundary:
    def test_ascii_word_does_not_match_inside_snake_case(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("agent-reach", ["search"]))
        result = registry.match("检索优先 web_search，其次知识库")
        assert result.matched == [], "search 不应命中 web_search（下划线连接符）"

    def test_ascii_word_does_not_match_longer_word(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("agent-reach", ["search"]))
        assert registry.match("进行 researching 工作").matched == []
        assert registry.match("searcher 工具").matched == []

    def test_ascii_word_matches_standalone(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("agent-reach", ["search"]))
        assert [s.name for s in registry.match("please search this").matched] == ["agent-reach"]
        assert [s.name for s in registry.match("Search").matched] == ["agent-reach"]

    def test_cjk_trigger_matches_substring(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("visual-clarify", ["视觉风格"]))
        assert [s.name for s in registry.match("帮我定视觉风格").matched] == ["visual-clarify"]

    def test_regex_trigger_unchanged(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("re-skill", ["re:搜.*索"]))
        assert [s.name for s in registry.match("请搜索一下").matched] == ["re-skill"]

    def test_empty_input_no_match(self) -> None:
        registry = SkillRegistry()
        registry.register(_skill("agent-reach", ["search"]))
        assert registry.match("").matched == []
