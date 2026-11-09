# 功能：市场包体系 — 组织架构蓝图注册（5 个内置蓝图）

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchitectureBlueprint:
    """组织架构蓝图——纯结构定义，不含运行时配置。"""

    id: str
    name: str
    description: str
    category: str
    collaboration_pattern: str
    dag_summary: str
    roles: list[dict[str, Any]]
    tags: list[str] = field(default_factory=list)
    team_size: str = ""
    emoji: str = ""
    color: str = ""
    industry: str = ""
    use_cases: list[str] = field(default_factory=list)

    def to_display_card(self) -> dict[str, Any]:
        """前端浏览卡片——与现有 WS 格式对齐。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "team_size": self.team_size,
            "emoji": self.emoji,
            "color": self.color,
            "roles_count": len(self.roles),
            "collaboration_pattern": self.collaboration_pattern,
            "dag_summary": self.dag_summary,
        }

    def to_detail(self) -> dict[str, Any]:
        """前端详情预览——含完整 roles 和 templates。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "collaboration_pattern": self.collaboration_pattern,
            "dag_summary": self.dag_summary,
            "tags": self.tags,
            "team_size": self.team_size,
            "emoji": self.emoji,
            "color": self.color,
            "roles": [
                {
                    "id": r["id"],
                    "name": r.get("name", r["id"]),
                    "responsibility": r.get("responsibility", ""),
                    "reports_to": r.get("reports_to", "owner"),
                    "can_spawn": r.get("can_spawn", []),
                }
                for r in self.roles
            ],
        }


# ---------------------------------------------------------------------------

ARCHITECTURE_PRESETS: list[ArchitectureBlueprint] = [
    # ── Startup Studio ───────────────────────────────────────────────────
    ArchitectureBlueprint(
        id="startup-studio",
        name="Startup Studio",
        description="Lean full-stack startup team. CEO sets strategy, CTO drives tech decisions, engineers and designers build in parallel, QA validates.",
        category="startup",
        collaboration_pattern="hub_spoke",
        dag_summary="CEO→CTO→[Eng∥Des]→QA→Review→Deploy",
        tags=["full-stack", "agile", "mvp", "small-team"],
        team_size="3-8",
        emoji="\U0001f680",
        color="#3498db",
        roles=[
            {
                "id": "ceo",
                "name": "CEO",
                "responsibility": "Product vision, strategy, and final decisions",
                "reports_to": "owner",
                "can_spawn": ["cto"],
            },
            {
                "id": "cto",
                "name": "CTO",
                "responsibility": "Technical architecture and engineering leadership",
                "reports_to": "ceo",
                "can_spawn": ["engineer", "designer"],
            },
            {
                "id": "engineer",
                "name": "Engineer",
                "responsibility": "Full-stack development and implementation",
                "reports_to": "cto",
                "can_spawn": [],
            },
            {
                "id": "designer",
                "name": "Designer",
                "responsibility": "UI/UX design, prototyping, and user research",
                "reports_to": "cto",
                "can_spawn": [],
            },
            {
                "id": "qa",
                "name": "QA Engineer",
                "responsibility": "Testing, quality assurance, and bug tracking",
                "reports_to": "cto",
                "can_spawn": [],
            },
        ],
    ),
    # ── Enterprise Corp ──────────────────────────────────────────────────
    ArchitectureBlueprint(
        id="enterprise-corp",
        name="Enterprise Corporation",
        description="Formal corporate hierarchy with department heads, approval gates, and compliance checkpoints. Suited for regulated industries.",
        category="enterprise",
        collaboration_pattern="hierarchical",
        dag_summary="PM→CEO✓→[Tech∥UX]→Impl→QA→Security✓→Prep→Release✓",
        tags=["corporate", "compliance", "governance", "large-team"],
        team_size="10-50",
        emoji="\U0001f3e2",
        color="#2c3e50",
        roles=[
            {
                "id": "ceo",
                "name": "CEO",
                "responsibility": "Executive leadership, vision, investor relations",
                "reports_to": "owner",
                "can_spawn": ["vp_eng", "vp_product", "cfo"],
            },
            {
                "id": "vp_eng",
                "name": "VP Engineering",
                "responsibility": "Engineering organization leadership",
                "reports_to": "ceo",
                "can_spawn": ["tech_lead", "devops_lead"],
            },
            {
                "id": "vp_product",
                "name": "VP Product",
                "responsibility": "Product strategy and roadmap",
                "reports_to": "ceo",
                "can_spawn": ["product_manager", "ux_lead"],
            },
            {
                "id": "cfo",
                "name": "CFO",
                "responsibility": "Finance, budgeting, compliance",
                "reports_to": "ceo",
                "can_spawn": [],
            },
            {
                "id": "tech_lead",
                "name": "Tech Lead",
                "responsibility": "Technical execution and code quality",
                "reports_to": "vp_eng",
                "can_spawn": ["engineer"],
            },
            {
                "id": "devops_lead",
                "name": "DevOps Lead",
                "responsibility": "Infrastructure, CI/CD, monitoring",
                "reports_to": "vp_eng",
                "can_spawn": [],
            },
            {
                "id": "product_manager",
                "name": "Product Manager",
                "responsibility": "Feature specs, user stories, prioritization",
                "reports_to": "vp_product",
                "can_spawn": [],
            },
            {
                "id": "ux_lead",
                "name": "UX Lead",
                "responsibility": "User experience design and research",
                "reports_to": "vp_product",
                "can_spawn": ["designer"],
            },
            {
                "id": "engineer",
                "name": "Software Engineer",
                "responsibility": "Development and implementation",
                "reports_to": "tech_lead",
                "can_spawn": [],
            },
            {
                "id": "designer",
                "name": "UI Designer",
                "responsibility": "Visual design and prototyping",
                "reports_to": "ux_lead",
                "can_spawn": [],
            },
            {
                "id": "qa_lead",
                "name": "QA Lead",
                "responsibility": "Quality assurance strategy and testing",
                "reports_to": "vp_eng",
                "can_spawn": [],
            },
        ],
    ),
    # ── Creative Agency ──────────────────────────────────────────────────
    ArchitectureBlueprint(
        id="creative-agency",
        name="Creative Agency",
        description="Client-driven creative team. Account manager handles client relations, creative director sets the vision, specialists execute across disciplines.",
        category="agency",
        collaboration_pattern="review_loop",
        dag_summary="Brief→Concept→[Visual∥Copy]→Build→Review✓→Client✓",
        tags=["creative", "client-work", "design", "marketing"],
        team_size="5-15",
        emoji="\U0001f3a8",
        color="#e74c3c",
        roles=[
            {
                "id": "account_manager",
                "name": "Account Manager",
                "responsibility": "Client relations, project scoping, deliverable tracking",
                "reports_to": "owner",
                "can_spawn": ["creative_director"],
            },
            {
                "id": "creative_director",
                "name": "Creative Director",
                "responsibility": "Creative vision, brand consistency, quality standards",
                "reports_to": "account_manager",
                "can_spawn": ["designer", "copywriter", "developer"],
            },
            {
                "id": "designer",
                "name": "Visual Designer",
                "responsibility": "Graphics, layouts, brand assets, UI mockups",
                "reports_to": "creative_director",
                "can_spawn": [],
            },
            {
                "id": "copywriter",
                "name": "Copywriter",
                "responsibility": "Copy, content strategy, messaging, tone of voice",
                "reports_to": "creative_director",
                "can_spawn": [],
            },
            {
                "id": "developer",
                "name": "Web Developer",
                "responsibility": "Frontend development, CMS, landing pages",
                "reports_to": "creative_director",
                "can_spawn": [],
            },
        ],
    ),
    # ── Research Lab ─────────────────────────────────────────────────────
    ArchitectureBlueprint(
        id="research-lab",
        name="Research Lab",
        description="Academic-style research team. Principal investigator leads hypothesis-driven research with peer review and reproducibility checks.",
        category="research",
        collaboration_pattern="pipeline",
        dag_summary="Hypothesis→LitReview→Design→[Data∥Experiment]→Analysis→PeerReview✓→Publish",
        tags=["academic", "research", "data-science", "peer-review"],
        team_size="3-10",
        emoji="\U0001f52c",
        color="#9b59b6",
        roles=[
            {
                "id": "pi",
                "name": "Principal Investigator",
                "responsibility": "Research direction, hypothesis formulation, publication oversight",
                "reports_to": "owner",
                "can_spawn": ["researcher", "data_engineer"],
            },
            {
                "id": "researcher",
                "name": "Research Scientist",
                "responsibility": "Experiment design, analysis, paper writing",
                "reports_to": "pi",
                "can_spawn": [],
            },
            {
                "id": "data_engineer",
                "name": "Data Engineer",
                "responsibility": "Data pipelines, infrastructure, reproducibility",
                "reports_to": "pi",
                "can_spawn": [],
            },
            {
                "id": "reviewer",
                "name": "Peer Reviewer",
                "responsibility": "Critical review, methodology validation, feedback",
                "reports_to": "pi",
                "can_spawn": [],
            },
        ],
    ),
    # ── DevOps Pipeline ──────────────────────────────────────────────────
    ArchitectureBlueprint(
        id="devops-pipeline",
        name="DevOps Pipeline",
        description="Infrastructure-focused team optimized for CI/CD, monitoring, and rapid deployment cycles with SRE practices.",
        category="engineering",
        collaboration_pattern="pipeline",
        dag_summary="Plan→Dev→CI→[Security∥Tests]→Prep→Canary✓→Production",
        tags=["devops", "sre", "infrastructure", "automation"],
        team_size="4-12",
        emoji="⚙️",
        color="#27ae60",
        roles=[
            {
                "id": "sre_lead",
                "name": "SRE Lead",
                "responsibility": "Reliability strategy, incident response, SLO management",
                "reports_to": "owner",
                "can_spawn": ["platform_eng", "security_eng"],
            },
            {
                "id": "platform_eng",
                "name": "Platform Engineer",
                "responsibility": "Infrastructure as code, Kubernetes, cloud architecture",
                "reports_to": "sre_lead",
                "can_spawn": [],
            },
            {
                "id": "security_eng",
                "name": "Security Engineer",
                "responsibility": "Security audits, vulnerability scanning, compliance",
                "reports_to": "sre_lead",
                "can_spawn": [],
            },
            {
                "id": "developer",
                "name": "Developer",
                "responsibility": "Application development and feature delivery",
                "reports_to": "sre_lead",
                "can_spawn": [],
            },
        ],
    ),
]

# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def get_all_presets() -> list[ArchitectureBlueprint]:
    """返回全部内置架构蓝图。"""
    return list(ARCHITECTURE_PRESETS)


def get_preset(preset_id: str) -> ArchitectureBlueprint | None:
    """按 ID 查找单个蓝图。"""
    for p in ARCHITECTURE_PRESETS:
        if p.id == preset_id:
            return p
    return None


def get_preset_categories() -> list[str]:
    """返回所有蓝图的去重分类（已排序）。"""
    return sorted({p.category for p in ARCHITECTURE_PRESETS})


# ---------------------------------------------------------------------------


def _role_to_dict(role: dict[str, Any]) -> dict[str, Any]:
    """将 RoleProfile 或 dict 统一转为 blueprint roles 格式。"""
    from bandi.company.company_runtime_profiles import RoleProfile

    if isinstance(role, RoleProfile):
        return {
            "id": role.id,
            "name": role.name,
            "responsibility": role.responsibility,
            "reports_to": role.reports_to or "",
            "can_spawn": list(role.can_spawn),
        }
    return dict(role)
