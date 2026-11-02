from __future__ import annotations

from bandi.api.company_chat import (
    _mention_name_map,
    _resolve_registered_role,
    resolve_mentions,
)
from bandi.company.org_engine import OrgEmployee, OrgEngine


def _engine() -> OrgEngine:
    org = OrgEngine()
    org.register_role("visual", "UI 视觉设计师")
    org.register_employee(
        OrgEmployee(employee_id="emp-visual", name="视觉设计师甲", role_id="visual")
    )
    return org


def test_spaced_display_name_mention_resolves_and_is_scrubbed() -> None:
    org = _engine()
    names = _mention_name_map(org, {"visual"})
    result = resolve_mentions("@UI 视觉设计师 帮我看下首页", ["visual"], names)
    assert result.target_agents == ["visual"]
    assert "@UI" not in result.clean_message
    assert "帮我看下首页" in result.clean_message


def test_employee_name_mention_maps_to_role() -> None:
    org = _engine()
    names = _mention_name_map(org, {"visual"})
    result = resolve_mentions("@视觉设计师甲 出一版方案", ["visual"], names)
    assert result.target_agents == ["visual"]
    assert "出一版方案" in result.clean_message


def test_dm_target_accepts_employee_name_and_id() -> None:
    org = _engine()
    assert _resolve_registered_role("视觉设计师甲", org) == "visual"
    assert _resolve_registered_role("emp-visual", org) == "visual"
