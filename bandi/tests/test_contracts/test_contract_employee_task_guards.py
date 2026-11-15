from __future__ import annotations

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


def _create_role(role_id: str, team_id: str = "") -> None:
    body = {"id": role_id, "name": role_id}
    if team_id:
        body["team_id"] = team_id
    resp = client.post("/api/company/roles", json=body)
    assert resp.status_code == 200, resp.text


def _create_employee(employee_id: str, role_id: str, name: str = "测试员工") -> None:
    resp = client.post(
        "/api/company/employees",
        json={"employee_id": employee_id, "name": name, "role_id": role_id},
    )
    assert resp.status_code == 200, resp.text


class TestPurgeTaskGuards:
    def test_purge_blocks_queued_work_item(self) -> None:
        from bandi.api.app import _org_api_dispatcher
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        _create_role("qa-task-role")
        _create_employee("qa-task-emp", "qa-task-role")
        dispatcher = _org_api_dispatcher()
        item = create_work_item(
            "待执行任务",
            WorkItemTurnType.EXECUTE,
            assignee_role="qa-task-role",
        )
        dispatcher.store.save(item)
        try:
            resp = client.post("/api/company/employees/qa-task-emp/purge", json={"confirm": True})
            assert resp.status_code == 409
            assert "ACTIVE_TASKS" in resp.text
            assert client.get("/api/company/employees/qa-task-emp").status_code == 200
        finally:
            dispatcher.store.delete(item.id)

    def test_purge_blocks_pending_approval_without_work_item(self) -> None:
        from bandi.api.app import _org_api_dispatcher
        from bandi.tools.approval import PendingApproval, get_approval_store

        _create_role("qa-pending-role")
        _create_employee("qa-pending-emp", "qa-pending-role")
        store = get_approval_store()
        session_id = "role-runtime::qa-run::qa-pending-role"
        store.set_pending(
            session_id,
            PendingApproval(
                tool_call_id="call-1",
                tool_name="write",
                params_digest="digest",
                reason="需要审批",
            ),
        )
        try:
            resp = client.post(
                "/api/company/employees/qa-pending-emp/purge", json={"confirm": True}
            )
            assert resp.status_code == 409
            assert "APPROVAL_PENDING" in resp.text
            assert client.get("/api/company/employees/qa-pending-emp").status_code == 200
        finally:
            store.clear_session(session_id)
        _ = _org_api_dispatcher()

    def test_purge_blocks_pending_interaction_without_work_item(self) -> None:
        from bandi.api.app import _get_interaction_store, _org_api_dispatcher

        _create_role("qa-interaction-role")
        _create_employee("qa-interaction-emp", "qa-interaction-role")
        insights = _get_interaction_store()
        session_id = "role-runtime::qa-run::qa-interaction-role"
        item = insights.register(session_id=session_id, kind="confirm", payload={"q": "继续？"})
        try:
            resp = client.post(
                "/api/company/employees/qa-interaction-emp/purge", json={"confirm": True}
            )
            assert resp.status_code == 409
            assert "INTERACTION_PENDING" in resp.text
        finally:
            insights.reject(item.interaction_id)
        _ = _org_api_dispatcher()

    def test_unrelated_pending_does_not_block_purge(self) -> None:
        from bandi.tools.approval import PendingApproval, get_approval_store

        _create_role("qa-unrelated-role")
        _create_employee("qa-unrelated-emp", "qa-unrelated-role")
        store = get_approval_store()
        store.set_pending(
            "unrelated-session",
            PendingApproval(
                tool_call_id="call-u",
                tool_name="write",
                params_digest="digest",
                reason="无关审批",
            ),
        )
        try:
            resp = client.post(
                "/api/company/employees/qa-unrelated-emp/purge", json={"confirm": True}
            )
            assert resp.status_code == 200, resp.text
        finally:
            store.clear_session("unrelated-session")


class TestDissolveTaskGuard:
    def test_dissolve_blocks_running_work_item(self) -> None:
        from bandi.api.app import _org_api_dispatcher
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        _create_role("qa-dissolve-role", team_id="qa-dissolve-team")
        dispatcher = _org_api_dispatcher()
        item = create_work_item(
            "运行中任务",
            WorkItemTurnType.EXECUTE,
            assignee_role="qa-dissolve-role",
            phase=Phase.RUNNING,
        )
        dispatcher.store.save(item)
        try:
            resp = client.delete("/api/company/org-teams/qa-dissolve-team")
            assert resp.status_code == 409
            assert "TEAM_RUNNING" in resp.text
            assert dispatcher.org_engine.get_role("qa-dissolve-role").team_id == "qa-dissolve-team"
        finally:
            dispatcher.store.delete(item.id)


class TestPurgeCompensation:
    def test_purge_retry_completes_after_save_failure(self, tmp_path: Path) -> None:
        from bandi.api.app import _company_operations, _org_api_dispatcher
        from bandi.company import org_persistence

        _create_role("qa-comp-role")
        _create_employee("qa-comp-emp", "qa-comp-role")
        dispatcher = _org_api_dispatcher()
        real_save = org_persistence.save_org
        real_persist = dispatcher.org_engine._persist_callable

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("save failed")

        dispatcher.org_engine._persist_callable = _boom
        try:
            with patch.object(org_persistence, "save_org", _boom):
                resp = client.post(
                    "/api/company/employees/qa-comp-emp/purge", json={"confirm": True}
                )
            assert resp.status_code == 500
        finally:
            dispatcher.org_engine._persist_callable = real_persist

        failed = _company_operations().list("failed")
        assert failed, "失败操作必须写入 company_operations"
        op_id = failed[-1]["op_id"]
        with patch.object(org_persistence, "save_org", real_save):
            retry = client.post(f"/api/company/operations/{op_id}/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["state"] == "committed"
        assert client.get("/api/company/employees/qa-comp-emp").status_code == 404
        tombstone = client.get("/api/company/employee-tombstones/qa-comp-emp")
        assert tombstone.status_code == 200
        org_json = tmp_path / ".bandi" / "company" / "org.json"
        if org_json.exists():
            assert "qa-comp-emp" not in org_json.read_text(encoding="utf-8")
