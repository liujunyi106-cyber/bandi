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
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _create_role(role_id: str, name: str = "测试岗位") -> None:
    resp = client.post("/api/company/roles", json={"id": role_id, "name": name})
    assert resp.status_code == 200, resp.text


def _create_employee(employee_id: str, role_id: str, name: str = "测试员工") -> None:
    resp = client.post(
        "/api/company/employees",
        json={"employee_id": employee_id, "name": name, "role_id": role_id},
    )
    assert resp.status_code == 200, resp.text


class TestEmployeeLifecycle:
    def test_duplicate_role_rejected(self) -> None:
        _create_role("role-dup")
        _create_employee("emp-a", "role-dup", "员工A")
        resp = client.post(
            "/api/company/employees",
            json={"employee_id": "emp-b", "name": "员工B", "role_id": "role-dup"},
        )
        assert resp.status_code == 409
        assert "role" in str(resp.json().get("message", "")).lower()

    def test_purge_removes_employee_role_profile_and_writes_tombstone(self) -> None:
        _create_role("role-purge", "待删除岗位")
        _create_employee("emp-purge", "role-purge", "待删除员工")

        resp = client.post("/api/company/employees/emp-purge/purge", json={"confirm": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["purged"] is True
        assert body["tombstone"]["employee_id"] == "emp-purge"
        assert body["tombstone"]["role_id"] == "role-purge"

        employees = client.get("/api/company/employees?include_inactive=true").json()["employees"]
        assert not any(item["employee_id"] == "emp-purge" for item in employees)
        roles = client.get("/api/company/roles").json()["roles"]
        assert not any(item["id"] == "role-purge" for item in roles)
        profile = client.get("/api/company/employees/emp-purge/profile")
        assert profile.status_code == 404

        tombstone = client.get("/api/company/employee-tombstones/emp-purge")
        assert tombstone.status_code == 200
        assert tombstone.json()["tombstone"]["name"] == "待删除员工"

    def test_tombstone_blocks_old_ids_but_allows_same_name_new_id(self) -> None:
        _create_role("role-old", "同名岗位")
        _create_employee("emp-old", "role-old", "同名员工")
        assert (
            client.post("/api/company/employees/emp-old/purge", json={"confirm": True}).status_code
            == 200
        )

        old_employee = client.post(
            "/api/company/employees",
            json={"employee_id": "emp-old", "name": "同名员工", "role_id": "role-new"},
        )
        assert old_employee.status_code == 409

        _create_role("role-new", "同名岗位")
        old_role = client.post(
            "/api/company/employees",
            json={"employee_id": "emp-new", "name": "同名员工", "role_id": "role-old"},
        )
        assert old_role.status_code == 409

        new_employee = client.post(
            "/api/company/employees",
            json={"employee_id": "emp-new", "name": "同名员工", "role_id": "role-new"},
        )
        assert new_employee.status_code == 200, new_employee.text

    def test_purge_is_idempotent(self) -> None:
        _create_role("role-idem")
        _create_employee("emp-idem", "role-idem")
        first = client.post("/api/company/employees/emp-idem/purge", json={"confirm": True})
        second = client.post("/api/company/employees/emp-idem/purge", json={"confirm": True})
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["tombstone"]["employee_id"] == "emp-idem"

    def test_session_system_note_is_persistent_and_idempotent(self) -> None:
        created = client.post("/api/sessions", json={"session_id": "s-note", "title": "提示测试"})
        assert created.status_code == 200, created.text
        body = {
            "content": "已解散团队",
            "note_kind": "team_dissolved",
            "dedupe_key": "team_dissolved:team-x",
        }
        first = client.post("/api/sessions/s-note/system-notes", json=body)
        second = client.post("/api/sessions/s-note/system-notes", json=body)
        assert first.status_code == 200
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        detail = client.get("/api/sessions/s-note")
        assert detail.status_code == 200
        notes = [
            item
            for item in detail.json()["messages"]
            if item.get("role") == "system" and item.get("content") == "已解散团队"
        ]
        assert len(notes) == 1

    def test_team_dissolve_marks_project_and_keeps_member_dm_capable(self) -> None:
        from bandi.company.team_registry import TeamRegistry

        project = client.post(
            "/api/company/projects",
            json={"project_id": "p-dissolve", "name": "解散测试项目"},
        )
        assert project.status_code == 200, project.text
        _create_role("role-team-a", "团队A")
        _create_role("role-team-b", "团队B")
        client.patch("/api/company/roles/role-team-a", json={"team_id": "team-a"})
        client.patch("/api/company/roles/role-team-b", json={"team_id": "team-a"})
        added = client.post("/api/company/projects/p-dissolve/teams", json={"team_id": "team-a"})
        assert added.status_code == 200, added.text
        _create_employee("emp-team-a", "role-team-a", "成员A")
        _create_employee("emp-team-b", "role-team-b", "成员B")

        dissolved = client.delete("/api/company/org-teams/team-a")
        assert dissolved.status_code == 200, dissolved.text
        assert dissolved.json()["dissolved"] is True
        assert TeamRegistry().get_status("team-a") == "dissolved"
        again = client.delete("/api/company/org-teams/team-a")
        assert again.status_code == 200
        assert again.json()["dissolved"] is True

        detail = client.get("/api/company/projects")
        record = next(
            item for item in detail.json()["projects"] if item["project_id"] == "p-dissolve"
        )
        assert record["team_ids"] == []
        assert record["dissolved_team_ids"] == ["team-a"]
        assert client.get("/api/company/employees/emp-team-a").status_code == 200

    def test_team_blackboard_is_scoped_by_team_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company.comms import read_team_memory, write_team_memory
        from bandi.company.team_registry import TeamRegistry

        monkeypatch.setenv("BANDI_HOME", str(tmp_path))
        workspace = str(tmp_path / "workspace")
        write_team_memory(workspace, "p1", "s1", "team-a", team_id="team-a")
        assert read_team_memory(workspace, "p1", "s1", team_id="team-a") == "team-a"
        assert "team-a" not in read_team_memory(workspace, "p1", "s1", team_id="team-b")

        TeamRegistry().set_status("team-a", "dissolved")
        assert read_team_memory(workspace, "p1", "s1", team_id="team-a") == ""
        with pytest.raises(ValueError, match="TEAM_DISSOLVED"):
            write_team_memory(workspace, "p1", "s1", "blocked", team_id="team-a")


class TestOperationsAndMigration:
    def test_company_operations_audit_states(self, tmp_path: Path) -> None:
        from bandi.company.company_operations import CompanyOperationsStore

        store = CompanyOperationsStore(tmp_path / "ops.db")
        ok = store.begin("purge", "emp-audit")
        store.commit(ok)
        bad = store.begin("dissolve", "team-audit")
        store.fail(bad, "boom\nsecret")
        item = store.get(bad)
        assert item is not None
        assert item["state"] == "failed"
        assert item["actor"] == "local"
        assert "\n" not in item["error"]
        assert {entry["kind"] for entry in store.list()} == {"purge", "dissolve"}

    def test_company_operations_actor_migration_and_stats(self, tmp_path: Path) -> None:
        import sqlite3

        from bandi.company.company_operations import CompanyOperationsStore

        path = tmp_path / "ops-old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE company_operations ("
            " op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,"
            " state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO company_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old-op", "purge", "emp-old", "failed", "boom", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()

        store = CompanyOperationsStore(path)
        old = store.get("old-op")
        assert old is not None
        assert old["actor"] == "local"
        op_id = store.begin("dissolve", "team-new", "cli")
        store.commit(op_id)
        stats = store.stats()
        assert stats["total"] == 2
        assert stats["by_state"] == {"failed": 1, "committed": 1}
        assert stats["by_kind"] == {"purge": 1, "dissolve": 1}
        assert store.get(op_id)["actor"] == "cli"

    def test_operations_stats_endpoint(self) -> None:
        from bandi.company.employee_lifecycle import EmployeeTombstoneStore

        EmployeeTombstoneStore().add(
            employee_id="qa-stats-emp", role_id="qa-stats-role", name="统计员工"
        )
        resp = client.get("/api/company/operations/stats")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tombstones"] >= 1
        assert "by_state" in body["operations"]
        assert body["team_pending"] >= 0

    def test_thread_system_note_dedupes_and_persists_metadata(self) -> None:
        from bandi.api.app import _get_company_dispatcher
        from bandi.company.discussion_thread import ThreadManager

        threads = _get_company_dispatcher().threads
        thread = threads.create_thread(participants=["user", "qa-note-role"])
        body = {
            "content": "该员工已退役",
            "note_kind": "employee_retired",
            "dedupe_key": "employee_retired:qa-note-role",
        }
        first = client.post(f"/api/company/threads/{thread.thread_id}/system-notes", json=body)
        second = client.post(f"/api/company/threads/{thread.thread_id}/system-notes", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        assert second.json()["created"] is False

        manager = ThreadManager(
            workspace=str(Path.home() / ".bandi" / "company"),
            project_id="company",
            session_id="default",
        )
        manager.hydrate()
        reloaded = manager.get_thread(thread.thread_id)
        assert reloaded is not None
        notes = [message for message in reloaded.messages if message.sender == "system"]
        assert len(notes) == 1
        assert notes[0].metadata["note_kind"] == "employee_retired"
        assert notes[0].metadata["dedupe_key"] == "employee_retired:qa-note-role"

    def test_purge_does_not_cascade_shared_skill_or_knowledge(self) -> None:
        from bandi.company.agent_profile import AgentProfileManager

        _create_role("qa-shared-r1")
        _create_role("qa-shared-r2")
        _create_employee("qa-shared-e1", "qa-shared-r1")
        _create_employee("qa-shared-e2", "qa-shared-r2")
        skill_dir = Path.home() / ".bandi" / "skills" / "qa-shared-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# 共享技能", encoding="utf-8")
        knowledge_dir = Path.home() / ".bandi" / "knowledge" / "qa-shared-kb"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        manager = AgentProfileManager()
        manager.bind_resource("qa-shared-e1", "general_skill", "qa-shared-skill")
        manager.bind_resource("qa-shared-e2", "general_skill", "qa-shared-skill")
        manager.bind_resource("qa-shared-e2", "knowledge_base", "qa-shared-kb")

        resp = client.post("/api/company/employees/qa-shared-e1/purge", json={"confirm": True})
        assert resp.status_code == 200, resp.text
        assert (skill_dir / "SKILL.md").is_file()
        assert knowledge_dir.is_dir()
        survivor = manager.get_profile("qa-shared-e2")
        assert survivor is not None
        assert any(
            item.resource_type.value == "general_skill" and item.resource_id == "qa-shared-skill"
            for item in survivor.resources
        )
        assert manager.get_profile("qa-shared-e1") is None

    def test_purge_without_role_id_succeeds(self) -> None:
        resp = client.post(
            "/api/company/employees",
            json={"employee_id": "qa-no-role", "name": "无岗位员工"},
        )
        assert resp.status_code == 200, resp.text
        purged = client.post("/api/company/employees/qa-no-role/purge", json={"confirm": True})
        assert purged.status_code == 200, purged.text
        assert purged.json()["role_deleted"] is False
        assert purged.json()["tombstone"]["role_id"] == ""
        assert client.get("/api/company/employees/qa-no-role").status_code == 404

    def test_legacy_team_memory_single_and_multi_team(self, tmp_path: Path) -> None:
        from bandi.company.comms import migrate_legacy_team_memory

        root = tmp_path / "comms"
        legacy = root / "_shared" / "team_memory" / "TEAM_MEMORY.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        result = migrate_legacy_team_memory(root, ["team-a"])
        assert result["state"] == "migrated"
        assert (root / "_shared" / "teams" / "team-a" / "TEAM_MEMORY.md").read_text(
            encoding="utf-8"
        ) == "legacy"
        assert not legacy.exists()

        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("old-multi", encoding="utf-8")
        result = migrate_legacy_team_memory(root, ["team-a", "team-b"])
        assert result["state"] == "deleted"
        assert not legacy.exists()

    def test_team_pending_cannot_confirm_after_dissolve(self) -> None:
        from bandi.company.team_registry import TeamRegistry

        TeamRegistry().set_status("team-gone", "dissolved")
        with patch(
            "bandi.api.app._get_team_staging_store",
        ) as factory:
            record = type(
                "Record",
                (),
                {"payload": {"team_id": "team-gone", "summary": "x", "project_id": "p"}},
            )()
            factory.return_value.get_pending.return_value = record
            resp = client.post("/api/memory/team-pending/p1/confirm", json={})
        assert resp.status_code == 409
        assert "TEAM_DISSOLVED" in str(resp.json())

    def test_template_rejects_second_employee_for_same_role(self) -> None:
        from bandi.company.org_engine import OrgEmployee
        from bandi.company.team_template import TeamRoleSpec, TeamTemplate
        from bandi.company.team_template_store import build_org_from_template

        dispatcher = __import__(
            "bandi.api.app", fromlist=["_org_api_dispatcher"]
        )._org_api_dispatcher()
        org = dispatcher.org_engine
        org.register_role("tpl-team_a", "模板岗位")
        org.register_employee(
            OrgEmployee(employee_id="tpl-old", name="旧员工", role_id="tpl-team_a")
        )
        template = TeamTemplate(
            template_id="tpl",
            name="模板",
            team_id="tpl-team",
            roles=[TeamRoleSpec(id="a", name="岗位A")],
        )
        from bandi.company.team_template import TeamTemplateError

        with pytest.raises(TeamTemplateError, match="ROLE_ALREADY_STAFFED"):
            build_org_from_template(template, org)

    def test_purge_writes_committed_operation(self) -> None:
        _create_role("role-op")
        _create_employee("emp-op", "role-op")
        resp = client.post("/api/company/employees/emp-op/purge", json={"confirm": True})
        assert resp.status_code == 200, resp.text
        op_id = resp.json()["op_id"]
        item = client.get("/api/company/operations").json()["operations"]
        assert any(entry["op_id"] == op_id and entry["state"] == "committed" for entry in item)
        assert client.get("/api/company/operations").text.find("persona") == -1

    def test_last_role_purge_auto_dissolves_team(self) -> None:
        from bandi.company.team_registry import TeamRegistry

        client.post("/api/company/projects", json={"project_id": "p-auto", "name": "自动解散"})
        _create_role("role-last")
        client.patch("/api/company/roles/role-last", json={"team_id": "team-last"})
        assert (
            client.post(
                "/api/company/projects/p-auto/teams", json={"team_id": "team-last"}
            ).status_code
            == 200
        )
        _create_employee("emp-last", "role-last")
        assert (
            client.post("/api/company/employees/emp-last/purge", json={"confirm": True}).status_code
            == 200
        )
        project = next(
            item
            for item in client.get("/api/company/projects").json()["projects"]
            if item["project_id"] == "p-auto"
        )
        assert project["team_ids"] == []
        assert project["dissolved_team_ids"] == ["team-last"]
        assert TeamRegistry().get_status("team-last") == "dissolved"

    def test_dissolve_one_team_keeps_other_project_team(self) -> None:
        client.post("/api/company/projects", json={"project_id": "p-two", "name": "双团队"})
        for team_id, role_id in (("team-a2", "role-a2"), ("team-b2", "role-b2")):
            _create_role(role_id)
            client.patch(f"/api/company/roles/{role_id}", json={"team_id": team_id})
            assert (
                client.post(
                    "/api/company/projects/p-two/teams", json={"team_id": team_id}
                ).status_code
                == 200
            )
        assert client.delete("/api/company/org-teams/team-a2").status_code == 200
        project = next(
            item
            for item in client.get("/api/company/projects").json()["projects"]
            if item["project_id"] == "p-two"
        )
        assert project["team_ids"] == ["team-b2"]
        assert project["dissolved_team_ids"] == ["team-a2"]

    def test_pending_approval_blocks_purge(self) -> None:
        _create_role("role-pending")
        _create_employee("emp-pending", "role-pending")
        with patch("bandi.api.app._employee_pending_block", return_value="APPROVAL_PENDING"):
            resp = client.post("/api/company/employees/emp-pending/purge", json={"confirm": True})
        assert resp.status_code == 409
        assert client.get("/api/company/employees/emp-pending").status_code == 200

    def test_readd_dissolved_team_restores_active(self) -> None:
        from bandi.company.team_registry import TeamRegistry

        client.post("/api/company/projects", json={"project_id": "p-readd", "name": "重拉"})
        for role_id, team_id in (("role-r1", "team-readd"), ("role-r2", "team-readd")):
            _create_role(role_id)
            client.patch(f"/api/company/roles/{role_id}", json={"team_id": team_id})
        assert (
            client.post(
                "/api/company/projects/p-readd/teams", json={"team_id": "team-readd"}
            ).status_code
            == 200
        )
        assert client.delete("/api/company/org-teams/team-readd").status_code == 200
        assert TeamRegistry().get_status("team-readd") == "dissolved"
        restored = client.post(
            "/api/company/projects/p-readd/teams", json={"team_id": "team-readd"}
        )
        assert restored.status_code == 200, restored.text
        assert TeamRegistry().get_status("team-readd") == "active"
        project = next(
            item
            for item in client.get("/api/company/projects").json()["projects"]
            if item["project_id"] == "p-readd"
        )
        assert "team-readd" in project["team_ids"]
        assert "team-readd" not in project["dissolved_team_ids"]
