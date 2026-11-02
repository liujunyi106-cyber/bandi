from __future__ import annotations

from typing import Any

BUILTIN_TALENT_TEMPLATES: list[dict[str, Any]] = [
    # ── Management ─────────────────────────────────────────────────
    {
        "id": "ceo-strategist",
        "name": "CEO Strategist",
        "description": "Executive leader who sets product vision, makes strategic decisions, and coordinates cross-functional teams.",
        "category": "management",
        "domains": ["strategy", "leadership", "product-vision"],
        "tags": ["executive", "decision-maker", "coordinator"],
    },
    {
        "id": "product-manager",
        "name": "Product Manager",
        "description": "Defines feature specs, writes user stories, prioritizes backlog, and bridges business and engineering.",
        "category": "product",
        "domains": ["product-strategy", "user-stories", "prioritization"],
        "tags": ["roadmap", "specs", "stakeholder"],
    },
    # ── Engineering ────────────────────────────────────────────────
    {
        "id": "cto-architect",
        "name": "CTO / Tech Architect",
        "description": "Technical leader who designs system architecture, makes technology choices, and mentors engineering teams.",
        "category": "engineering",
        "domains": ["architecture", "tech-leadership", "system-design"],
        "tags": ["technical", "architecture", "leadership"],
    },
    {
        "id": "fullstack-engineer",
        "name": "Full-Stack Engineer",
        "description": "Versatile developer handling frontend, backend, databases, and deployment. Ships features end-to-end.",
        "category": "engineering",
        "domains": ["frontend", "backend", "database", "deployment"],
        "tags": ["full-stack", "implementation", "coding"],
        "preferred_external_agent": "claude_code",
    },
    {
        "id": "devops-engineer",
        "name": "DevOps / Platform Engineer",
        "description": "Infrastructure specialist managing CI/CD pipelines, cloud architecture, Kubernetes, and deployment automation.",
        "category": "engineering",
        "domains": ["infrastructure", "ci-cd", "kubernetes", "cloud"],
        "tags": ["devops", "automation", "infrastructure"],
        "preferred_external_agent": "claude_code",
    },
    {
        "id": "security-engineer",
        "name": "Security Engineer",
        "description": "Security specialist performing audits, vulnerability scanning, compliance checks, and threat modeling.",
        "category": "engineering",
        "domains": ["security", "compliance", "vulnerability", "audit"],
        "tags": ["security", "compliance", "audit"],
    },
    {
        "id": "sre-lead",
        "name": "SRE Lead",
        "description": "Site reliability engineer managing SLOs, incident response, monitoring, and system resilience.",
        "category": "engineering",
        "domains": ["reliability", "monitoring", "incident-response", "slo"],
        "tags": ["sre", "oncall", "observability"],
    },
    # ── Design ─────────────────────────────────────────────────────
    {
        "id": "ui-ux-designer",
        "name": "UI/UX Designer",
        "description": "Creates user interfaces, prototypes, and conducts user research. Bridges user needs and visual design.",
        "category": "design",
        "domains": ["ui-design", "ux-research", "prototyping", "figma"],
        "tags": ["design", "user-experience", "visual"],
    },
    {
        "id": "creative-director",
        "name": "Creative Director",
        "description": "Sets creative vision, ensures brand consistency, and maintains quality standards across all creative output.",
        "category": "design",
        "domains": ["creative-direction", "brand", "quality"],
        "tags": ["creative", "vision", "brand"],
    },
    # ── Testing ────────────────────────────────────────────────────
    {
        "id": "qa-engineer",
        "name": "QA Engineer",
        "description": "Tests software quality through manual and automated testing, bug tracking, and regression analysis.",
        "category": "testing",
        "domains": ["testing", "quality-assurance", "automation", "bugs"],
        "tags": ["qa", "testing", "bugs"],
        "preferred_external_agent": "claude_code",
    },
    # ── Writing ────────────────────────────────────────────────────
    {
        "id": "copywriter",
        "name": "Copywriter",
        "description": "Crafts compelling copy, content strategy, messaging, and maintains consistent tone of voice.",
        "category": "writing",
        "domains": ["copywriting", "content-strategy", "messaging"],
        "tags": ["writing", "content", "creative"],
    },
    {
        "id": "editor-in-chief",
        "name": "Editor-in-Chief",
        "description": "Manages editorial strategy, content calendar, quality standards, and publishing cadence.",
        "category": "writing",
        "domains": ["editorial", "content-calendar", "publishing"],
        "tags": ["editorial", "publishing", "quality"],
    },
    # ── Research ───────────────────────────────────────────────────
    {
        "id": "research-scientist",
        "name": "Research Scientist",
        "description": "Designs experiments, analyzes data, writes papers, and pushes the boundaries of knowledge.",
        "category": "research",
        "domains": ["experiment-design", "data-analysis", "paper-writing"],
        "tags": ["research", "academic", "analysis"],
    },
    {
        "id": "data-engineer",
        "name": "Data Engineer",
        "description": "Builds data pipelines, manages infrastructure, and ensures data quality and reproducibility.",
        "category": "data",
        "domains": ["data-pipelines", "etl", "databases", "reproducibility"],
        "tags": ["data", "pipelines", "infrastructure"],
        "preferred_external_agent": "claude_code",
    },
    {
        "id": "principal-investigator",
        "name": "Principal Investigator",
        "description": "Leads research direction, formulates hypotheses, oversees publications, and mentors researchers.",
        "category": "research",
        "domains": ["research-direction", "hypothesis", "publication"],
        "tags": ["pi", "academic", "leadership"],
    },
    # ── Marketing ──────────────────────────────────────────────────
    {
        "id": "seo-specialist",
        "name": "SEO Specialist",
        "description": "Performs keyword research, optimizes content for search engines, and tracks analytics metrics.",
        "category": "marketing",
        "domains": ["seo", "keyword-research", "analytics"],
        "tags": ["seo", "marketing", "analytics"],
    },
    {
        "id": "social-media-manager",
        "name": "Social Media Manager",
        "description": "Manages social distribution, community engagement, cross-promotion, and audience growth.",
        "category": "marketing",
        "domains": ["social-media", "engagement", "distribution"],
        "tags": ["social", "marketing", "community"],
    },
]


def get_all_talent_presets() -> list[dict[str, Any]]:
    """返回全部内置人才模板。"""
    return list(BUILTIN_TALENT_TEMPLATES)


def get_talent_preset(template_id: str) -> dict[str, Any] | None:
    """按 ID 查找单个模板。"""
    for t in BUILTIN_TALENT_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


def get_talent_categories() -> list[str]:
    """返回所有模板的去重分类（已排序）。"""
    return sorted({t["category"] for t in BUILTIN_TALENT_TEMPLATES})
