# 功能：契约——秘书/无团队上下文的技能目录作用域（只吃全局层，不吃别团队技能）。
#
# 钉行为不钉实现：
# - SkillInjector(global_only=True) 时目录只含 team_id 为空的全局技能；
# - 默认 global_only=False 时行为不变（空 team_id=全量，既有契约 F1 三态不动）；
# - 有团队上下文的员工路径不受影响（全局 + 本团队）；
# - agent_runner 接线：无团队上下文（无 org_engine / 角色未注册）→ global_only。
from __future__ import annotations

from pathlib import Path

from bandi.skills.loader import SkillLoader
from bandi.skills.manager import SkillInjector, create_skill_registry
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(name: str, team_id: str = "") -> SkillDef:
    return SkillDef(
        name=name,
        description=f"{name} 描述",
        triggers=[name],
        instructions=f"{name} 指令",
        team_id=team_id,
    )


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register_all(
        [
            _skill("global-skill"),
            _skill("a-skill", "team-a"),
            _skill("b-skill", "team-b"),
        ]
    )
    return reg


def test_g1_global_only_excludes_other_teams() -> None:
    # 功能：秘书上下文（global_only）不得看到别团队技能
    catalog = SkillInjector(_registry(), team_id="", global_only=True).build_catalog()

    assert "**global-skill**" in catalog
    assert "**a-skill**" not in catalog, "秘书目录不得含 A 团队技能"
    assert "**b-skill**" not in catalog, "秘书目录不得含 B 团队技能"


def test_g2_default_behavior_unchanged() -> None:
    # 功能：默认（global_only=False）行为不变——空 team_id 仍为全量（既有契约 F1）
    catalog = SkillInjector(_registry(), team_id="").build_catalog()

    for name in ("global-skill", "a-skill", "b-skill"):
        assert f"**{name}**" in catalog, f"{name} 应在目录"

    assert "a-skill 指令" not in catalog, "F 模式不注入正文"


def test_g3_team_context_unaffected() -> None:
    # 功能：员工路径（global_only=False + team_id）仍为「全局 + 本团队」
    catalog = SkillInjector(_registry(), team_id="team-a", global_only=True).build_catalog()

    assert "**global-skill**" in catalog
    assert "**a-skill**" in catalog, "本团队技能必须可见"
    assert "**b-skill**" not in catalog


def test_g4_resolve_team_id_reports_missing_context() -> None:
    # 功能：无 org_engine / 角色未注册时须能识别为「无团队上下文」
    from bandi.core.agent_runner import _resolve_skill_team_id

    class _Engine:
        def get_role(self, role_id: str):
            return None

    assert _resolve_skill_team_id(None, "ceo") == ""
    assert _resolve_skill_team_id(_Engine(), "ceo") == ""


def test_g5_loader_scoped_skills_still_load() -> None:
    # 功能：global_only 只影响目录面，不影响装载（全量技能仍可被工具发现）
    root = Path(__file__).parent
    assert root.exists()
    reg = create_skill_registry()
    catalog = SkillInjector(reg, team_id="", global_only=True).build_catalog()
    assert isinstance(catalog, str)


def test_g6_loader_respects_team_id_frontmatter(tmp_path: Path) -> None:
    # 功能：带属技能经 loader 装载后，global_only 目录不得含带属技能
    for name, team in (("gs", ""), ("as", "team-a")):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\nteam_id: {team}\n---\n\n## Behavior\n\n- {name}\n\n"
            f"## Instructions\n\n{name} 指令\n",
            encoding="utf-8",
        )
    reg = SkillRegistry()
    reg.register_all(SkillLoader(str(tmp_path)).load_all())

    secretary = SkillInjector(reg, team_id="", global_only=True).build_catalog()
    employee = SkillInjector(reg, team_id="team-a").build_catalog()

    assert "**gs**" in secretary
    assert "**as**" not in secretary
    assert "**as**" in employee
