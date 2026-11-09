from __future__ import annotations

from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(name: str, *, description: str = "", instructions: str = "", **kw: object) -> SkillDef:
    return SkillDef(
        name=name,
        description=description,
        triggers=[],
        instructions=instructions,
        **kw,  # type: ignore[arg-type]
    )


class TestBuildSkillsSummary:
    def test_empty_registry(self) -> None:
        reg = SkillRegistry()
        assert reg.build_skills_summary() == "## Available Skills\nNo skills installed."

    def test_summary_line_for_normal_skill(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("demo", description="A demo skill"))
        out = reg.build_skills_summary()
        assert "## Available Skills" in out
        assert "demo" in out
        assert "A demo skill" in out

    def test_full_body_for_always_skill(self) -> None:
        reg = SkillRegistry()
        reg.register(
            _skill(
                "always-skill",
                description="always on",
                instructions="## Behavior\nDo the thing.",
                always=True,
            )
        )
        out = reg.build_skills_summary()
        assert "Do the thing." in out

    def test_normal_skill_body_not_injected(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("normal", description="n", instructions="## Behavior\nsecret body"))
        out = reg.build_skills_summary()
        assert "secret body" not in out

    def test_multiple_skills_all_listed(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("a", description="aa"))
        reg.register(_skill("b", description="bb"))
        out = reg.build_skills_summary()
        assert "a" in out and "b" in out
