from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._company_operations_store", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _dissolved_project(project_id: str = "qa-dissolved") -> None:
    from bandi.company.projects import ProjectRecord, ProjectStore

    ProjectStore().save_project(
        ProjectRecord(
            project_id=project_id,
            name="已解散项目",
            team_ids=[],
            dissolved_team_ids=["qa-team"],
        )
    )


class TestProjectAccessGuard:
    def test_rest_chat_blocks_dissolved_project(self) -> None:
        _dissolved_project()
        resp = client.post(
            "/api/agent/chat",
            json={"message": "还在吗", "project_id": "qa-dissolved"},
        )
        assert resp.status_code == 409
        assert "TEAM_DISSOLVED" in resp.text

    def test_rest_stream_blocks_dissolved_project(self) -> None:
        _dissolved_project()
        resp = client.post(
            "/api/agent/stream",
            json={"message": "还在吗", "project_id": "qa-dissolved"},
        )
        assert resp.status_code == 409
        assert "TEAM_DISSOLVED" in resp.text

    def test_session_ws_blocks_dissolved_project(self) -> None:
        _dissolved_project()
        with client.websocket_connect("/api/agent/stream?token=test") as ws:
            ws.send_json(
                {
                    "type": "chat",
                    "message": "还在吗",
                    "session_id": "qa-ws-dissolved",
                    "project_id": "qa-dissolved",
                }
            )
            frame = ws.receive_json()
        assert frame.get("code") == "TEAM_DISSOLVED"
        assert frame.get("data") == "TEAM_DISSOLVED"

    def test_secretary_session_and_active_team_are_not_blocked(self) -> None:
        from bandi.api.app import _get_store, _project_access_block
        from bandi.company.employee_lifecycle import EmployeeTombstoneStore
        from bandi.company.projects import ProjectRecord, ProjectStore

        _dissolved_project()
        store = _get_store()
        store.create_session(session_id="qa-secretary", origin="secretary")
        assert _project_access_block("qa-dissolved", "qa-secretary") == ""
        assert _project_access_block("qa-dissolved", "qa-session", "qa-member-role") == ""

        EmployeeTombstoneStore().add(
            employee_id="qa-retired", role_id="qa-retired-role", name="退役员工"
        )
        assert (
            _project_access_block("qa-dissolved", "qa-session", "qa-retired-role")
            == "EMPLOYEE_RETIRED"
        )

        ProjectStore().save_project(
            ProjectRecord(
                project_id="qa-active",
                name="仍在运行",
                team_ids=["qa-team-b"],
                dissolved_team_ids=["qa-team-a"],
            )
        )
        assert _project_access_block("qa-active", "qa-session") == ""

        ProjectStore().save_project(ProjectRecord(project_id="qa-never", name="从未拉入团队"))
        assert _project_access_block("qa-never", "qa-session") == ""

    def test_default_assignee_excludes_paused_member(self) -> None:
        from bandi.api.app import _org_api_dispatcher, _pick_top_role, _project_member_roles

        client.post("/api/company/projects", json={"project_id": "qa-assign-p", "name": "承接项目"})
        for role_id, final in (("qa-top", True), ("qa-worker", False)):
            client.post(
                "/api/company/roles",
                json={
                    "id": role_id,
                    "name": role_id,
                    "team_id": "qa-assign-team",
                    "is_final_decider": final,
                },
            )
        client.post("/api/company/projects/qa-assign-p/teams", json={"team_id": "qa-assign-team"})
        for employee_id, role_id in (("qa-top-emp", "qa-top"), ("qa-worker-emp", "qa-worker")):
            client.post(
                "/api/company/employees",
                json={"employee_id": employee_id, "name": employee_id, "role_id": role_id},
            )
        paused = client.put("/api/company/employees/qa-top-emp", json={"status": "paused"})
        assert paused.status_code == 200, paused.text

        org_engine = _org_api_dispatcher().org_engine
        members = _project_member_roles("qa-assign-p", org_engine)
        assert [role.id for role in members] == ["qa-worker"]
        assert _pick_top_role(members) == "qa-worker"
