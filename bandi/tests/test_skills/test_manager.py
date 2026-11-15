from __future__ import annotations

from bandi.skills.manager import SkillInjector, create_skill_registry
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry


def _make_registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register(
        SkillDef(
            name="code-review",
            description="审查代码变更",
            triggers=["审查代码", "code review"],
            instructions="你是一个代码审查专家。\n- 检查逻辑错误\n- 检查安全漏洞",
            allowed_tools=["grep_tool", "read_file", "bash"],
            constraints=["不要修改代码", "只分析不执行"],
        )
    )
    r.register(
        SkillDef(
            name="web-research",
            description="网络调研",
            triggers=["调研", "research", "搜索"],
            instructions="你是一个网络调研专家。\n- 从多个来源交叉验证\n- 标注可信度",
        )
    )
    return r


class TestSkillCatalog:
    def test_catalog_lists_all_skills_without_body(self):
        injection = SkillInjector(_make_registry()).build_catalog()
        assert "## 可用技能" in injection
        assert "code-review" in injection
        assert "web-research" in injection
        assert "审查代码变更" in injection
        assert "代码审查专家" not in injection, "正文不得进目录"
        assert "不要修改代码" not in injection, "约束不得进目录"

    def test_catalog_marks_bound_skills_first(self):
        catalog = SkillInjector(_make_registry(), bound_names=["web-research"]).build_catalog()
        lines = [line for line in catalog.splitlines() if line.startswith("- **")]
        assert "岗位推荐" in lines[0], "绑定技能优先展示"
        assert "web-research" in lines[0]

    def test_catalog_empty_registry(self):
        assert SkillInjector(SkillRegistry()).build_catalog() == ""

    def test_create_skill_registry_empty(self):
        registry = create_skill_registry("/tmp/nonexistent_skills_dir_xyz")
        assert registry.get_count() == 0
