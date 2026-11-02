# 功能：契约测试——组织团队导出为团队模板快照（结构还原 + 可再实例化）
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    app_mod = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(app_mod, "_API_KEY", "test")
    import bandi.llm.models_dev as md_mod

    monkeypatch.setattr(md_mod, "trigger_models_dev_sync", lambda: None)
    app_mod._company_dispatcher = None
    app_mod._project_store = None

    from fastapi.testclient import TestClient

    client = TestClient(app_mod.app, headers={"Authorization": "Bearer test"})
    yield client
    app_mod._company_dispatcher = None
    app_mod._project_store = None


def _dispatcher() -> Any:
    from bandi.api.app import _org_api_dispatcher

    return _org_api_dispatcher()


def _seed_team(api: Any) -> None:
    """功能：建一个两角色团队（带 team 前缀 + 汇报链 + 工具面）。"""
    lead = api.post(
        "/api/company/roles",
        json={
            "id": "t-alpha_lead",
            "name": "负责人",
            "responsibility": "拍板",
            "team_id": "t-alpha",
            "can_spawn": ["t-alpha_kid"],
            "tool_tags": ["bash", "write"],
            "is_final_decider": True,
        },
    )
    assert lead.status_code == 200, lead.text
    kid = api.post(
        "/api/company/roles",
        json={
            "id": "t-alpha_kid",
            "name": "执行",
            "responsibility": "干活",
            "reports_to": "t-alpha_lead",
            "team_id": "t-alpha",
        },
    )
    assert kid.status_code == 200, kid.text


def test_export_strips_prefix_and_keeps_hierarchy(api: Any) -> None:
    _seed_team(api)

    response = api.get("/api/company/org-teams/t-alpha/export")
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers.get("content-disposition", "")

    template = response.json()
    assert template["template_id"] == "t-alpha"
    assert template["team_id"] == ""
    roles = {role["id"]: role for role in template["roles"]}
    assert set(roles) == {"lead", "kid"}
    assert roles["kid"]["reports_to"] == "lead"
    assert roles["lead"]["can_spawn"] == ["kid"]
    assert roles["lead"]["tool_tags"] == ["bash", "write"]
    assert roles["lead"]["is_final_decider"] is True


def test_export_missing_team_is_404(api: Any) -> None:
    response = api.get("/api/company/org-teams/ghost-team/export")
    assert response.status_code == 404
    assert "not found" in response.text


def test_export_carries_per_role_llm_config(api: Any) -> None:
    _seed_team(api)
    from bandi.company.org_engine import OrgEmployee

    _dispatcher().org_engine.register_employee(
        OrgEmployee(
            employee_id="t-alpha_lead",
            name="负责人",
            role_id="t-alpha_lead",
            llm_config={"default_model": "deepseek-flash", "thinking_mode": "high"},
        )
    )

    template = api.get("/api/company/org-teams/t-alpha/export").json()
    lead = next(role for role in template["roles"] if role["id"] == "lead")
    assert lead["llm_config"]["default_model"] == "deepseek-flash"
    assert lead["llm_config"]["thinking_mode"] == "high"


def test_exported_snapshot_can_be_instantiated_again(api: Any) -> None:
    """破坏者视角：导出物不是"看起来像模板"，改名后必须能真的实例化出平行团队。"""
    _seed_team(api)
    template = api.get("/api/company/org-teams/t-alpha/export").json()

    from bandi.company.team_template import TeamTemplate
    from bandi.company.team_template_store import build_org_from_template

    parsed = TeamTemplate.model_validate(template)
    cloned = parsed.model_copy(update={"team_id": "t-beta"})
    report = build_org_from_template(cloned, _dispatcher().org_engine)
    assert report.created_role_ids == ["t-beta_lead", "t-beta_kid"]
    assert report.warnings == []

    cloned_export = api.get("/api/company/org-teams/t-beta/export").json()
    assert {role["id"] for role in cloned_export["roles"]} == {"lead", "kid"}
