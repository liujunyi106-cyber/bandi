# 功能：契约测试——人才模板 hire 直接落团队 + 团队模板列表计数
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

    yield TestClient(app_mod.app, headers={"Authorization": "Bearer test"})
    app_mod._company_dispatcher = None
    app_mod._project_store = None


def test_hire_with_team_assigns_role_to_team(api: Any) -> None:
    hired = api.post(
        "/api/company/talent/hire",
        json={
            "template_name": "writer",
            "name": "文案甲",
            "role_id": "t-copy_writer",
            "team_id": "t-copy",
        },
    )
    assert hired.status_code == 200, hired.text

    roles = api.get("/api/company/roles").json()["roles"]
    writer = next(role for role in roles if role["id"] == "t-copy_writer")
    assert writer["team_id"] == "t-copy"


def test_hire_existing_role_keeps_config_and_switches_team(api: Any) -> None:
    api.post("/api/company/roles", json={"id": "t-move_dev", "name": "开发", "team_id": "t-old"})
    hired = api.post(
        "/api/company/talent/hire",
        json={
            "template_name": "general_engineer",
            "name": "开发乙",
            "role_id": "t-move_dev",
            "employee_id": "t-move_dev",
            "team_id": "t-new",
        },
    )
    assert hired.status_code == 200, hired.text
    roles = api.get("/api/company/roles").json()["roles"]
    assert next(role for role in roles if role["id"] == "t-move_dev")["team_id"] == "t-new"
    employees = api.get("/api/company/employees").json()["employees"]
    assert any(item["employee_id"] == "t-move_dev" for item in employees)


def test_team_templates_list_exposes_role_count_only(api: Any) -> None:
    """模板只建组织：列表保留 role_count，任务图/评审门计数不再存在。"""
    teams = api.get("/api/company/teams").json()["teams"]
    selection = next(item for item in teams if item["template_id"] == "selection-team")
    assert selection["role_count"] == 5
    assert "work_item_count" not in selection
    assert "gate_count" not in selection


def test_instantiate_accepts_custom_team_name(api: Any) -> None:
    """团队名称与团队 ID 解耦：名称可中文，ID 仍作员工 id 前缀。"""
    created = api.post(
        "/api/company/teams/product-team/instantiate",
        json={"team_id": "t-named", "team_name": "我的产品组"},
    )
    assert created.status_code == 200, created.text
    teams = api.get("/api/company/org-teams").json()["teams"]
    named = next(item for item in teams if item["team_id"] == "t-named")
    assert named["name"] == "我的产品组"
    assert named["role_count"] == 5
