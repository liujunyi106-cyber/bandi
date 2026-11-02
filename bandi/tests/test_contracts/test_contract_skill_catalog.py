# 功能：契约——技能常驻目录（正文只在 skill_invoke 时加载）。
#
# 钉行为不钉实现：
# - 每轮 system prompt 常驻「name + description」短目录（不常驻正文）；
# - 触发词命中也不自动注入正文（F 模式，正文由工具按需返回）；
# - 目录受团队作用域约束（秘书只见到全局层技能）；
# - 目录有预算：单项 description 截断 + 总预算不足时降级为 names-only；
# - 无技能时目录为空（不得输出空壳标题）。
from __future__ import annotations

from pathlib import Path

from bandi.skills.loader import SkillLoader
from bandi.skills.manager import SkillInjector
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _skill(
    name: str, description: str = "", body: str = "", team_id: str = "", triggers=None
) -> SkillDef:
    return SkillDef(
        name=name,
        description=description or f"{name} 描述",
        triggers=list(triggers or []),
        instructions=body or f"{name} 正文指令",
        team_id=team_id,
    )


def _injector(
    skills: list[SkillDef], team_id: str = "", global_only: bool = False
) -> SkillInjector:
    reg = SkillRegistry()
    reg.register_all(skills)
    return SkillInjector(reg, team_id=team_id, global_only=global_only)


class TestCatalogAlwaysPresent:
    def test_c1_catalog_present_without_match(self) -> None:
        # 功能：目录每轮常驻（模型据此知道能调 skill_invoke）
        catalog = _injector([_skill("alpha", "甲技能"), _skill("beta", "乙技能")]).build_catalog()

        assert catalog, "目录不得为空"
        assert "alpha" in catalog
        assert "beta" in catalog

    def test_c2_catalog_never_contains_body(self) -> None:
        # 功能：目录只放 name + description，绝不放正文（省 token 的全部来源）
        catalog = _injector([_skill("alpha", "甲技能", "SECRET_BODY_MARKER")]).build_catalog()

        assert "SECRET_BODY_MARKER" not in catalog
        assert "alpha" in catalog

    def test_c3_trigger_match_does_not_add_body(self) -> None:
        # 功能：触发词命中也不再自动注入正文（F 模式）
        catalog = _injector(
            [_skill("alpha", "甲技能", "ALPHA_BODY", triggers=["触发甲"]), _skill("beta", "乙技能")]
        ).build_catalog()

        assert "alpha" in catalog and "beta" in catalog
        assert "ALPHA_BODY" not in catalog

    def test_c4_catalog_respects_global_only(self) -> None:
        # 功能：秘书目录不得含别团队技能（作用域约束同样作用于目录）
        catalog = _injector(
            [
                _skill("global-one", "全局技能"),
                _skill("team-a-one", "A 团队技能", team_id="team-a"),
                _skill("team-b-one", "B 团队技能", team_id="team-b"),
            ],
            team_id="",
            global_only=True,
        ).build_catalog()

        assert "global-one" in catalog
        assert "team-a-one" not in catalog
        assert "team-b-one" not in catalog

    def test_c5_catalog_respects_team_scope(self) -> None:
        # 功能：员工目录 = 全局 + 本团队
        catalog = _injector(
            [
                _skill("global-one", "全局技能"),
                _skill("team-a-one", "A 团队技能", team_id="team-a"),
                _skill("team-b-one", "B 团队技能", team_id="team-b"),
            ],
            team_id="team-a",
        ).build_catalog()

        assert "global-one" in catalog
        assert "team-a-one" in catalog
        assert "team-b-one" not in catalog

    def test_c6_empty_registry_no_catalog_shell(self) -> None:
        # 功能：无技能时目录为空字符串（不输出空壳标题）
        assert _injector([]).build_catalog() == ""


class TestCatalogBudget:
    def test_c7_long_description_truncated(self) -> None:
        # 功能：单项 description 超长须截断
        long_desc = "描" * 900
        catalog = _injector([_skill("alpha", long_desc)]).build_catalog()

        assert long_desc not in catalog
        assert "alpha" in catalog
        assert len(catalog) < 1200, "单项超长 description 未被截断"

    def test_c8_many_skills_bounded(self) -> None:
        # 功能：技能很多时目录有总量上限（不得随技能数线性膨胀到不可控）
        skills = [_skill(f"skill-{i:03}", f"描述{i}") for i in range(200)]
        catalog = _injector(skills).build_catalog()

        assert len(catalog) <= 6000, f"目录未受总预算约束：{len(catalog)} 字符"

    def test_c9_degrades_to_names_only_when_budget_tight(self) -> None:
        # 功能：预算极紧时降级为 names-only（保留可发现性，丢弃描述）
        skills = [_skill(f"skill-{i:03}", "描述" * 120) for i in range(120)]
        catalog = _injector(skills).build_catalog()

        assert "skill-000" in catalog, "降级后仍须保留技能名"
        assert len(catalog) <= 6000


class TestCatalogRendering:
    def test_c10_assembler_renders_catalog(self) -> None:
        from bandi.perception.context_assembler import ContextAssembler

        build = ContextAssembler._build_skills_artifact
        catalog = _injector(
            [_skill("alpha", "甲技能", "ALPHA_BODY"), _skill("beta", "乙技能")]
        ).build_catalog()

        artifact = build(catalog)

        assert artifact is not None
        assert "alpha" in artifact.content and "beta" in artifact.content
        assert "ALPHA_BODY" not in artifact.content, "正文不得渲染进工件"

    def test_c11_assembler_none_when_empty(self) -> None:
        from bandi.perception.context_assembler import ContextAssembler

        assert ContextAssembler._build_skills_artifact("") is None


class TestCatalogLoaderPath:
    def test_c12_real_loader_skills_expose_catalog(self, tmp_path: Path) -> None:
        # 功能：真实 loader 装载的 opencode 风格技能也能出现在目录里
        skill_dir = tmp_path / "demo-web"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-web\ndescription: 把网页转成干净 markdown\n---\n\n# Demo\n\n正文。\n",
            encoding="utf-8",
        )
        reg = SkillRegistry()
        reg.register_all(SkillLoader(str(tmp_path)).load_all())

        catalog = SkillInjector(reg, team_id="").build_catalog()

        assert "demo-web" in catalog
        assert "把网页转成干净 markdown" in catalog
