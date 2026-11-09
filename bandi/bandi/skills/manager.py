# 功能：技能目录注入（SkillInjector 渲染常驻索引，正文由 skill_invoke 按需加载）
from __future__ import annotations

from bandi.skills.loader import SkillLoader
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry

# 功能：技能索引预算
_CATALOG_DESC_CAP = 250
_CATALOG_BUDGET_CHARS = 6000
_BOUND_BADGE = "（岗位推荐）"


def _shorten(text: str, cap: int) -> str:
    """功能：描述按字符上限截断（超限补省略号）。"""
    value = " ".join((text or "").split())
    if cap <= 0 or len(value) <= cap:
        return value
    return value[: max(cap - 1, 1)] + "…"


def create_skill_registry(skills_dir: str | None = None) -> SkillRegistry:
    # 10.4：缺省走 SkillLoader 目录级联（项目 .bandi/skills → 用户 ~/.bandi/skills）；
    # 显式目录保持单目录语义
    registry = SkillRegistry()
    loader = SkillLoader(skills_dir)
    skills = loader.load_all()
    registry.register_all(skills)
    return registry


class SkillInjector:
    """功能：渲染当前作用域可见的技能目录。

    ``global_only`` 只在**无团队上下文**（``team_id`` 为空）时生效：此时把可见面从
    「全量」收窄为「仅全局技能」，避免秘书/普通会话看到别团队技能。``team_id`` 非空
    时本就按「全局 + 本团队」过滤，该开关不参与。

    ``bound_names``：员工档案绑定的技能名（general_skill）——目录里标「岗位推荐」
    并排在最前；正文只在模型调用 ``skill_invoke`` 时加载。
    """

    def __init__(
        self,
        registry: SkillRegistry,
        team_id: str = "",
        global_only: bool = False,
        bound_names: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._team_id = str(team_id or "").strip()
        self._global_only = global_only
        self._bound_names = {str(n).strip() for n in (bound_names or []) if str(n).strip()}

    def _visible(self) -> list[SkillDef]:
        """功能：当前作用域内可见的技能（目录口径）。"""
        allowed = {s.name for s in self._registry.collect_for_team(self._team_id)}
        if self._global_only and not self._team_id:
            allowed &= {s.name for s in self._registry.collect_for_team("") if not s.team_id}
        return [s for s in self._registry.collect_for_team(self._team_id) if s.name in allowed]

    def _label(self, skill: SkillDef) -> str:
        """功能：目录项名字（岗位绑定技能带推荐标记）。"""
        if skill.name in self._bound_names:
            return f"{skill.name}{_BOUND_BADGE}"
        return skill.name

    def build_catalog(self) -> str:
        """功能：渲染常驻索引——``## 可用技能`` + 每行 ``- **name**: description``。

        岗位绑定技能排在最前；三级预算降级（单项描述硬截断 → 总预算内均摊 →
        names-only）。正文永不进索引，由 ``skill_invoke`` 按需加载。
        """
        skills = sorted(self._visible(), key=lambda s: (s.name not in self._bound_names, s.name))
        if not skills:
            return ""
        header = "## 可用技能\n\n需要某个技能的完整指令时，调用 skill_invoke 并传入技能名。"
        budget = _CATALOG_BUDGET_CHARS
        for cap in (_CATALOG_DESC_CAP, _CATALOG_DESC_CAP // 2, 0):
            lines = [
                f"- **{self._label(s)}**: {_shorten(s.description, cap)}"
                if cap
                else f"- **{self._label(s)}**"
                for s in skills
            ]
            text = header + "\n" + "\n".join(lines)
            if len(text) <= budget:
                return text
        return header + "\n" + "\n".join(f"- **{self._label(s)}**" for s in skills)[:budget]
