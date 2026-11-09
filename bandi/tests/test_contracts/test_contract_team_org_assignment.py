# 功能：契约测试——岗位组织关系更新 / 团队拉入与解散 / 无岗位员工与 metadata。
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


def test_role_patch_sets_team_and_rejects_cycle(api: Any) -> None:
    assert api.post("/api/company/roles", json={"id": "a", "name": "A"}).status_code == 200
    assert (
        api.post("/api/company/roles", json={"id": "b", "name": "B", "reports_to": "a"}).status_code
        == 200
    )

    patched = api.patch(
        "/api/company/roles/b",
        json={"team_id": "t1", "can_spawn": ["a"], "responsibility": "执行"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["team_id"] == "t1"
    assert patched.json()["can_spawn"] == ["a"]
    assert patched.json()["responsibility"] == "执行"

    cycle = api.patch("/api/company/roles/a", json={"reports_to": "b"})
    assert cycle.status_code == 400, cycle.text
    assert "cycle" in cycle.text.lower(), cycle.text

    assert api.patch("/api/company/roles/missing", json={}).status_code == 404


def test_team_pull_roles_and_dissolve_keeps_roles_and_employees(api: Any) -> None:
    api.post("/api/company/roles", json={"id": "r1", "name": "R1"})
    api.post("/api/company/roles", json={"id": "r2", "name": "R2"})
    created = api.post(
        "/api/company/employees",
        json={"employee_id": "e1", "name": "员工一", "role_id": "r1"},
    )
    assert created.status_code == 200, created.text

    pulled = api.post("/api/company/teams/t1/roles", json={"role_ids": ["r1", "r2"]})
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["role_ids"] == ["r1", "r2"]

    again = api.post("/api/company/teams/t1/roles", json={"role_ids": ["r1"]})
    assert again.status_code == 200, "重复拉入必须幂等（同团队同角色）"

    teams = api.get("/api/company/org-teams").json()["teams"]
    team = next(item for item in teams if item["team_id"] == "t1")
    assert {role["role_id"] for role in team["roles"]} == {"r1", "r2"}

    dissolved = api.delete("/api/company/org-teams/t1")
    assert dissolved.status_code == 200, dissolved.text
    assert dissolved.json()["detached_roles"] == 2

    teams_after = api.get("/api/company/org-teams").json()["teams"]
    assert all(item["team_id"] != "t1" for item in teams_after)

    roles = api.get("/api/company/roles").json()["roles"]
    assert next(role for role in roles if role["id"] == "r1")["team_id"] == ""
    employees = api.get("/api/company/employees?include_inactive=true").json()["employees"]
    assert any(item["employee_id"] == "e1" and item["role_id"] == "r1" for item in employees)
    repeated = api.delete("/api/company/org-teams/t1")
    assert repeated.status_code == 200
    assert repeated.json()["dissolved"] is True


def test_employee_without_role_and_metadata_roundtrip(api: Any) -> None:
    created = api.post(
        "/api/company/employees",
        json={
            "employee_id": "e2",
            "name": "待分配员工",
            "role_id": "",
            "description": "临时人设",
            "metadata": {
                "persona_prompt": "只做验证",
                "avatar": {"kind": "tone", "tone": 2},
            },
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["role_id"] == ""
    assert body["metadata"]["persona_prompt"] == "只做验证"
    assert body["metadata"]["avatar"]["tone"] == 2

    updated = api.put(
        "/api/company/employees/e2",
        json={"metadata": {"persona_prompt": "更新后的人设"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["metadata"]["persona_prompt"] == "更新后的人设"
    assert updated.json()["metadata"]["avatar"]["kind"] == "tone", "metadata 合并不得丢字段"

    purged = api.delete("/api/company/employees/e2")
    assert purged.status_code == 200
    assert purged.json()["purged"] is True
    assert purged.json()["tombstone"]["employee_id"] == "e2"
    assert api.get("/api/company/employees/e2").status_code == 404
