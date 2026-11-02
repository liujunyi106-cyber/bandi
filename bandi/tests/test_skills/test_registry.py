from __future__ import annotations

from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _make_skill(name: str, triggers: list[str], desc: str = "") -> SkillDef:
    return SkillDef(
        name=name,
        description=desc or f"Skill {name}",
        triggers=triggers,
        instructions=f"You are {name}.",
    )


class TestSkillRegistry:
    def test_register_and_get(self):
        reg = SkillRegistry()
        skill = _make_skill("reviewer", ["review code", "code review"])
        reg.register(skill)
        assert reg.get("reviewer") is skill
        assert reg.get("nonexistent") is None

    def test_register_all(self):
        reg = SkillRegistry()
        skills = [
            _make_skill("a", ["trigger a"]),
            _make_skill("b", ["trigger b"]),
        ]
        reg.register_all(skills)
        assert reg.get_count() == 2

    def test_remove(self):
        reg = SkillRegistry()
        reg.register(_make_skill("x", ["trigger x"]))
        reg.remove("x")
        assert reg.get("x") is None
        assert reg.get_count() == 0

    def test_list_all(self):
        reg = SkillRegistry()
        skills = [_make_skill(f"s{i}", [f"t{i}"]) for i in range(3)]
        reg.register_all(skills)
        result = reg.list_all()
        assert len(result) == 3

    def test_match_keyword(self):
        reg = SkillRegistry()
        reg.register(_make_skill("greeter", ["hello", "greet"]))
        reg.register(_make_skill("coder", ["write code", "coding"]))
        result = reg.match("Hello, can you greet me?")
        assert result.level == 2
        assert len(result.matched) == 1
        assert result.matched[0].name == "greeter"

    def test_match_regex_trigger(self):
        reg = SkillRegistry()
        reg.register(_make_skill("pr_reviewer", ["re:review\\s+(pr|merge|pull)"]))
        result = reg.match("please review pr #42")
        assert len(result.matched) == 1
        assert result.matched[0].name == "pr_reviewer"

    def test_match_multiple(self):
        reg = SkillRegistry()
        reg.register(_make_skill("a", ["hello"]))
        reg.register(_make_skill("b", ["hello"]))
        result = reg.match("say hello")
        assert len(result.matched) == 2

    def test_no_match(self):
        reg = SkillRegistry()
        reg.register(_make_skill("coder", ["coding", "programming"]))
        result = reg.match("what is the weather?")
        assert len(result.matched) == 0

    def test_empty_input(self):
        reg = SkillRegistry()
        reg.register(_make_skill("s", ["trigger"]))
        result = reg.match("")
        assert result.level == 1
        assert len(result.matched) == 0

    def test_bulk_register_from_skills_dir(self):
        reg = SkillRegistry()
        count = reg.bulk_register("/nonexistent/path")
        assert count == 0
        assert reg.get_count() == 0

    def test_get_name_only_view(self):
        reg = SkillRegistry()
        reg.register(_make_skill("a", ["ta"], "Skill A"))
        reg.register(_make_skill("b", ["tb"], "Skill B"))
        view = reg.get_name_only_view()
        assert len(view) == 2
        entry = {v["name"]: v["description"] for v in view}
        assert entry["a"] == "Skill A"
        assert entry["b"] == "Skill B"

    def test_invalid_regex_fallback(self):
        reg = SkillRegistry()
        reg.register(_make_skill("bad", ["re:[invalid"]))
        result = reg.match("anything")
        assert len(result.matched) == 0

    def test_case_insensitive_match(self):
        reg = SkillRegistry()
        reg.register(_make_skill("greeter", ["HELLO", "Greet Me"]))
        result = reg.match("hello, can you greet me?")
        assert len(result.matched) == 1
