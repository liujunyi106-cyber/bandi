# 功能：技能注册表（技能登记/查询）
from __future__ import annotations

import logging
import re

from bandi.skills.models import SkillDef, SkillMatchResult

logger = logging.getLogger(__name__)


class SkillRegistry:
    LEVEL_NAME_ONLY = 1
    LEVEL_FULL = 2

    def __init__(self):
        self._skills: dict[str, SkillDef] = {}

    def register(self, skill: SkillDef) -> None:
        self._skills[skill.name] = skill

    def register_all(self, skills: list[SkillDef]) -> None:
        for skill in skills:
            self.register(skill)

    def get(self, name: str) -> SkillDef | None:
        return self._skills.get(name)

    def remove(self, name: str) -> None:
        self._skills.pop(name, None)

    def list_all(self) -> list[SkillDef]:
        return list(self._skills.values())

    def get_count(self) -> int:
        return len(self._skills)

    def match(self, user_input: str) -> SkillMatchResult:
        if not user_input:
            return SkillMatchResult(matched=[], level=self.LEVEL_NAME_ONLY)
        matched: list[SkillDef] = []
        for skill in self._skills.values():
            for trigger in skill.triggers:
                if self._match_trigger(trigger, user_input):
                    matched.append(skill)
                    break
        return SkillMatchResult(matched=matched, level=self.LEVEL_FULL)

    def get_name_only_view(self) -> list[dict[str, str]]:
        return [{"name": s.name, "description": s.description} for s in self._skills.values()]

    def list_by_tags(self, tags: list[str]) -> list[SkillDef]:
        if not tags:
            return []
        wanted = {t.strip().lower() for t in tags if t.strip()}
        if not wanted:
            return []
        return [s for s in self._skills.values() if wanted.intersection(t.lower() for t in s.tags)]

    def collect_for_team(self, team_id: str) -> list[SkillDef]:
        """功能：按团队收集技能（=A 三态）。

        team_id 空 = 无团队上下文 → 不过滤（全量）；非空 = 全局技能（team_id 空）
        + 本团队技能；他团队技能不返回（A 团队配置不污染 B 团队）。
        """
        tid = str(team_id or "").strip()
        if not tid:
            return list(self._skills.values())
        return [s for s in self._skills.values() if not s.team_id or s.team_id == tid]

    def build_skills_summary(self) -> str:
        """构建技能摘要注入文本：always 技能全量 body，其余一行摘要。"""

        if not self._skills:
            return "## Available Skills\nNo skills installed."
        lines = ["## Available Skills"]
        for skill in self._skills.values():
            if skill.always and skill.instructions:
                lines.append(f"\n## Skill: {skill.name}\n{skill.instructions}")
            elif skill.description:
                lines.append(f"- **{skill.name}**: {skill.description}")
            else:
                lines.append(f"- **{skill.name}**")
        return "\n".join(lines)

    def bulk_register(self, skills_dir: str) -> int:
        from bandi.skills.loader import SkillLoader

        loader = SkillLoader(skills_dir)
        skills = loader.load_all()
        self.register_all(skills)
        logger.info("Registered %d skills from %s", len(skills), skills_dir)
        return len(skills)

    @staticmethod
    def _match_trigger(trigger: str, user_input: str) -> bool:
        """功能：触发词命中判定——ASCII 词按词边界匹配（`search` 不命中
        `web_search`/`researching`），中文按子串匹配（词边界对 CJK 天然成立）。"""
        trigger_lower = trigger.lower().strip()
        input_lower = user_input.lower()
        if trigger_lower.startswith("re:") and len(trigger_lower) > 3:
            try:
                return bool(re.search(trigger_lower[3:], input_lower))
            except re.error:
                logger.warning("Invalid regex trigger: %s", trigger_lower[3:])
                return False
        pattern = rf"(?<![a-z0-9_]){re.escape(trigger_lower)}(?![a-z0-9_])"
        return bool(re.search(pattern, input_lower))
