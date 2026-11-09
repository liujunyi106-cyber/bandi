# 功能：人才市场——模板目录扫描/解析 + 搜索/分类目录

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TalentTemplate:
    name: str
    category: str
    description: str
    skills: list[str] = field(default_factory=list)
    system_prompt: str = ""
    id: str = ""
    domains: list[str] = field(default_factory=list)
    prompt_ref: str = ""
    team_id: str = ""


_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "engineering": "软件工程与开发执行能力",
    "analytics": "数据分析与洞察能力",
    "content": "内容创作与编辑能力",
    "design": "设计与原型能力",
    "management": "项目管理与协调能力",
    "quality": "质量保障与测试能力",
}

_PREFERRED_CATEGORY = "engineering"


class TalentMarket:
    def __init__(self, templates_dir: str = "~/.bandi/talent") -> None:
        self._dir = Path(templates_dir).expanduser()
        self._templates: dict[str, TalentTemplate] = self._scan()
        self._org_engine: object | None = None

    def bind_org_engine(self, org_engine: object) -> None:
        """绑定 OrgEngine 实例——list_installed_packages 从其读取已安装包列表。"""
        self._org_engine = org_engine

    def _scan(self) -> dict[str, TalentTemplate]:
        if not self._dir.exists():
            return self._builtin_templates()
        templates: dict[str, TalentTemplate] = {}
        for f in self._dir.glob("*.md"):
            template = self._parse_template(f)
            if template:
                templates[template.name] = template
        # 目录存在但为空/全部解析失败时，回退内置模板（与目录不存在行为一致）
        return templates or self._builtin_templates()

    def _parse_template(self, path: Path) -> TalentTemplate | None:
        try:
            text = path.read_text(encoding="utf-8")
            lines = text.split("\n")
            name = path.stem
            category = "general"
            description = ""
            skills: list[str] = []
            system_prompt = ""
            template_id = ""
            domains: list[str] = []
            prompt_ref = ""
            for line in lines:
                if line.startswith("category:"):
                    category = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                elif line.startswith("skills:"):
                    raw_skills = line.split(":", 1)[1].strip()
                    skills = [s.strip() for s in raw_skills.split(",") if s.strip()]
                elif line.startswith("system_prompt:"):
                    system_prompt = line.split(":", 1)[1].strip()
                elif line.startswith("id:"):
                    template_id = line.split(":", 1)[1].strip()
                elif line.startswith("domains:"):
                    raw_domains = line.split(":", 1)[1].strip()
                    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]
                elif line.startswith("prompt_ref:"):
                    prompt_ref = line.split(":", 1)[1].strip()
            return TalentTemplate(
                name=name,
                category=category,
                description=description,
                skills=skills,
                system_prompt=system_prompt,
                id=template_id or name,
                domains=domains,
                prompt_ref=prompt_ref,
            )
        except (OSError, UnicodeDecodeError):
            return None

    def _builtin_templates(self) -> dict[str, TalentTemplate]:
        templates: dict[str, TalentTemplate] = {
            "general_engineer": TalentTemplate(
                name="general_engineer",
                category=_PREFERRED_CATEGORY,
                description="通用软件工程师",
                skills=["coding", "debugging", "code_review"],
                system_prompt="You are a software engineer skilled in coding and debugging.",
            ),
            "data_analyst": TalentTemplate(
                name="data_analyst",
                category="analytics",
                description="数据分析师",
                skills=["sql", "analysis", "reporting"],
                system_prompt="You are a data analyst skilled in SQL queries and data analysis.",
            ),
            "writer": TalentTemplate(
                name="writer",
                category="content",
                description="内容创作者",
                skills=["writing", "editing", "research"],
                system_prompt="You are a content creator skilled in writing and editing.",
            ),
            "designer": TalentTemplate(
                name="designer",
                category="design",
                description="设计师",
                skills=["ui_design", "prototyping", "visual_design"],
                system_prompt="You are a designer skilled in UI/UX and visual design.",
            ),
            "planner": TalentTemplate(
                name="planner",
                category="management",
                description="项目经理",
                skills=["planning", "scheduling", "risk_management"],
                system_prompt="You are a project manager skilled in planning and coordination.",
            ),
            "reviewer": TalentTemplate(
                name="reviewer",
                category="quality",
                description="代码审查员",
                skills=["code_review", "testing", "qa"],
                system_prompt="You are a code reviewer skilled in quality assurance.",
            ),
        }
        from .talent_presets import BUILTIN_TALENT_TEMPLATES

        for preset in BUILTIN_TALENT_TEMPLATES:
            tid = preset.get("id", "")
            if tid and tid not in templates:
                templates[tid] = TalentTemplate(
                    name=preset.get("name", tid),
                    category=preset.get("category", "general"),
                    description=preset.get("description", ""),
                    skills=[],
                    domains=list(preset.get("domains", []) or []),
                    team_id=preset.get("team_id", ""),
                )
        return templates

    def search(self, query: str, top_k: int = 3) -> list[TalentTemplate]:
        keywords = [kw for kw in query.lower().split() if kw]
        if not keywords:
            return []
        scores: list[tuple[int, TalentTemplate]] = []
        for template in self._templates.values():
            # 中英双通道：英文关键词匹配 name/skills，中文关键词匹配 description
            score = 0
            for kw in keywords:
                lowered = kw.lower()
                if lowered in template.name.lower():
                    score += 2
                elif any(lowered in skill.lower() for skill in template.skills):
                    score += 2
                elif lowered in template.description.lower():
                    score += 1
            scores.append((score, template))
        scores.sort(key=lambda x: -x[0])
        return [template for score, template in scores[:top_k] if score > 0]

    def list_templates(self) -> list[TalentTemplate]:
        return list(self._templates.values())

    def get_template(self, name: str) -> TalentTemplate | None:
        return self._templates.get(name)

    def describe_category(self, category: str) -> str:
        normalized = (category or "").strip().lower()
        if normalized in _CATEGORY_DESCRIPTIONS:
            return _CATEGORY_DESCRIPTIONS[normalized]
        return f"{normalized.replace('-', ' ').strip().title()} 相关能力与执行支持。"

    def build_employee_id(self, *, role_id: str, template_id: str) -> str:
        base = str(template_id or "").strip() or "employee"
        role_part = str(role_id or "").strip() or "any"
        return f"{base}-{role_part}-{uuid.uuid4().hex[:8]}"

    def build_candidate_summary(self, template: TalentTemplate) -> dict[str, str]:
        return {
            "template_id": template.id or template.name,
            "name": template.name,
            "description": template.description,
            "category": template.category,
            "category_description": self.describe_category(template.category),
        }

    def list_category_catalog(self) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        for template in self._templates.values():
            category = (template.category or "general").strip().lower() or "general"
            counts[category] = counts.get(category, 0) + 1
        catalog = [
            {
                "category": category,
                "description": self.describe_category(category),
                "template_count": count,
            }
            for category, count in counts.items()
        ]
        return sorted(
            catalog, key=lambda item: (item["category"] != _PREFERRED_CATEGORY, item["category"])
        )

    def list_templates_by_categories(self, *, categories: list[str]) -> list[TalentTemplate]:
        selected = {
            (category or "").strip().lower() for category in categories if str(category).strip()
        }
        if not selected:
            return []
        return [
            template
            for template in self._templates.values()
            if (template.category or "general").strip().lower() in selected
        ]

    def collect_for_team(self, team_id: str) -> list[TalentTemplate]:
        """按 team_id 过滤模板——team_id 为空时返回全部，非空时只返回匹配的。"""
        tid = str(team_id or "").strip()
        if not tid:
            return list(self._templates.values())
        return [t for t in self._templates.values() if str(t.team_id or "").strip() == tid]

    def list_installed_packages(self) -> list[dict[str, object]]:
        """返回已安装包记录列表（从绑定 OrgEngine 读取）。"""
        org_engine = getattr(self, "_org_engine", None)
        if org_engine is None:
            return []
        return list(getattr(org_engine, "installed_packages", []))
