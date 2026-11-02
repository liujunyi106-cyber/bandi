from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app, raise_server_exceptions=False)
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


class TestTeamRegistryPersistence:
    def test_status_and_members_survive_new_instance(self, tmp_path: Path) -> None:
        from bandi.company.team_registry import TeamRegistry

        registry = TeamRegistry(tmp_path / "team_names.json")
        registry.set("qa-team", "QA 团队")
        registry.set_status("qa-team", "dissolved")
        registry.set_members("qa-team", ["qa-r1", "qa-r2"])

        reloaded = TeamRegistry(tmp_path / "team_names.json")
        assert reloaded.get("qa-team") == "QA 团队"
        assert reloaded.get_status("qa-team") == "dissolved"
        assert reloaded.get_members("qa-team") == ["qa-r1", "qa-r2"]


class TestStartupReconcile:
    def test_pending_purge_is_committed_on_startup(self) -> None:
        from bandi.api.app import (
            _company_operations,
            _startup_reconcile_company_operations,
        )

        client.post("/api/company/roles", json={"id": "qa-reconcile-role", "name": "重放岗位"})
        client.post(
            "/api/company/employees",
            json={
                "employee_id": "qa-reconcile-emp",
                "name": "重放员工",
                "role_id": "qa-reconcile-role",
            },
        )
        assert (
            client.post(
                "/api/company/employees/qa-reconcile-emp/purge", json={"confirm": True}
            ).status_code
            == 200
        )
        op_id = _company_operations().begin("purge", "qa-reconcile-emp", "cli")
        asyncio.run(_startup_reconcile_company_operations())
        item = _company_operations().get(op_id)
        assert item is not None
        assert item["state"] == "committed"


class TestDissolveCompensation:
    def test_status_write_failure_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.api.app import _company_operations, _org_api_dispatcher
        from bandi.company.team_registry import TeamRegistry

        client.post(
            "/api/company/projects", json={"project_id": "qa-dissolve-p", "name": "重试项目"}
        )
        for role_id in ("qa-dissolve-r1", "qa-dissolve-r2"):
            client.post(
                "/api/company/roles",
                json={"id": role_id, "name": role_id, "team_id": "qa-dissolve-team"},
            )
        assert (
            client.post(
                "/api/company/projects/qa-dissolve-p/teams", json={"team_id": "qa-dissolve-team"}
            ).status_code
            == 200
        )
        dispatcher = _org_api_dispatcher()
        assert dispatcher.org_engine.get_role("qa-dissolve-r1").team_id == "qa-dissolve-team"

        real_set_status = TeamRegistry.set_status

        def _boom(self: TeamRegistry, team_id: str, status: str) -> None:
            raise RuntimeError("status write failed")

        monkeypatch.setattr(TeamRegistry, "set_status", _boom)
        failed = client.delete("/api/company/org-teams/qa-dissolve-team")
        assert failed.status_code == 500
        failed_ops = _company_operations().list("failed")
        assert failed_ops
        op_id = failed_ops[-1]["op_id"]

        monkeypatch.setattr(TeamRegistry, "set_status", real_set_status)
        retry = client.post(f"/api/company/operations/{op_id}/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["state"] == "committed"
        assert TeamRegistry().get_status("qa-dissolve-team") == "dissolved"
        assert dispatcher.org_engine.get_role("qa-dissolve-r1").team_id == ""
        project = next(
            item
            for item in client.get("/api/company/projects").json()["projects"]
            if item["project_id"] == "qa-dissolve-p"
        )
        assert "qa-dissolve-team" not in project["team_ids"]
        assert "qa-dissolve-team" in project["dissolved_team_ids"]


class TestMemoryIsolation:
    def test_team_blackboard_and_dm_do_not_leak(self, tmp_path: Path) -> None:
        from bandi.company.comms import write_team_memory
        from bandi.company.discussion_thread import ThreadManager

        workspace = str(tmp_path / "workspace")
        write_team_memory(workspace, "qa-p", "qa-s", "BLACKBOARD-ONLY", team_id="qa-team")
        manager = ThreadManager(workspace=workspace, project_id="qa-p", session_id="qa-s")
        thread = manager.create_thread(participants=["user", "qa-role"])
        manager.add_message(thread.thread_id, "user", "DM-ONLY")
        manager.add_message(thread.thread_id, "qa-role", "DM-REPLY")

        root = Path(workspace) / ".bandi-comms" / "qa-p" / "qa-s"
        blackboard = root / "_shared" / "teams" / "qa-team" / "TEAM_MEMORY.md"
        thread_file = root / "threads" / f"{thread.thread_id}.md"
        assert blackboard.read_text(encoding="utf-8") == "BLACKBOARD-ONLY"
        assert thread_file.is_file()
        thread_text = thread_file.read_text(encoding="utf-8")
        assert "DM-ONLY" in thread_text
        assert "BLACKBOARD-ONLY" not in thread_text


class TestProfileDeleteFailure:
    def test_profile_failure_keeps_employee_and_marks_operation_failed(self) -> None:
        from bandi.api.app import _company_operations
        from bandi.company.agent_profile import AgentProfileError

        client.post("/api/company/roles", json={"id": "qa-profile-role", "name": "档案岗位"})
        client.post(
            "/api/company/employees",
            json={
                "employee_id": "qa-profile-emp",
                "name": "档案员工",
                "role_id": "qa-profile-role",
            },
        )
        with patch(
            "bandi.company.agent_profile.AgentProfileManager.delete_profile",
            side_effect=AgentProfileError("delete failed"),
        ):
            resp = client.post(
                "/api/company/employees/qa-profile-emp/purge", json={"confirm": True}
            )
        assert resp.status_code == 500
        assert client.get("/api/company/employees/qa-profile-emp").status_code == 200
        failed = _company_operations().list("failed")
        assert any(item["target_id"] == "qa-profile-emp" for item in failed)
        tombstone = client.get("/api/company/employee-tombstones/qa-profile-emp")
        assert tombstone.status_code == 200
