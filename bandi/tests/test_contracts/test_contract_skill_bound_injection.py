# 功能：契约测试——员工档案绑定技能只进目录并标「岗位推荐」，不注入正文
from __future__ import annotations

from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(name: str, *, team_id: str = "") -> SkillDef:
    return SkillDef(
        name=name,
        description=f"{name} 描述",
        triggers=[],
        instructions=f"{name} 的完整指令",
        team_id=team_id,
    )


def _registry(*skills: SkillDef) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register_all(list(skills))
    return registry


class TestBoundSkillCatalog:
    def test_bound_skill_marked_and_no_body(self) -> None:
        injector = SkillInjector(
            _registry(_skill("pm-agile-workflow")),
            team_id="aid",
            bound_names=["pm-agile-workflow"],
        )
        catalog = injector.build_catalog()
        assert "pm-agile-workflow" in catalog
        assert "岗位推荐" in catalog
        assert "pm-agile-workflow 的完整指令" not in catalog, "绑定不得注入正文"

    def test_unbound_skill_in_catalog_without_badge(self) -> None:
        catalog = SkillInjector(
            _registry(_skill("pm-agile-workflow")), team_id="aid"
        ).build_catalog()
        assert "pm-agile-workflow" in catalog
        assert "岗位推荐" not in catalog

    def test_bound_skill_out_of_team_scope_hidden(self) -> None:
        """破坏者视角：绑定越权——绑定了他团队技能时目录也不得展示。"""
        injector = SkillInjector(
            _registry(_skill("other-team-skill", team_id="other")),
            team_id="aid",
            bound_names=["other-team-skill"],
        )
        assert "other-team-skill" not in injector.build_catalog()

    def test_bound_skill_sorted_first(self) -> None:
        injector = SkillInjector(
            _registry(_skill("a-skill"), _skill("z-bound")),
            team_id="aid",
            bound_names=["z-bound"],
        )
        lines = [line for line in injector.build_catalog().splitlines() if line.startswith("- **")]
        assert "z-bound" in lines[0], "岗位推荐技能排最前"
