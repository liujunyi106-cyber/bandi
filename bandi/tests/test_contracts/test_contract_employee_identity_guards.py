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


class TestRoleIdentityGuards:
    def test_talent_hire_rejects_second_employee_for_staffed_role(self) -> None:
        first = client.post(
            "/api/company/talent/hire",
            json={
                "template_name": "product-manager",
                "role_id": "qa-hire-role",
                "employee_id": "qa-hire-1",
            },
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/company/talent/hire",
            json={
                "template_name": "product-manager",
                "role_id": "qa-hire-role",
                "employee_id": "qa-hire-2",
            },
        )
        assert second.status_code == 409
        assert "staffed" in str(second.json()).lower()

    def test_talent_hire_rejects_tombstoned_role(self) -> None:
        from bandi.company.employee_lifecycle import EmployeeTombstoneStore

        EmployeeTombstoneStore().add(
            employee_id="qa-old-employee", role_id="qa-reserved-role", name="旧员工"
        )
        resp = client.post(
            "/api/company/talent/hire",
            json={
                "template_name": "product-manager",
                "role_id": "qa-reserved-role",
                "employee_id": "qa-new-employee",
            },
        )
        assert resp.status_code == 409
        assert "IDENTITY_RESERVED" in str(resp.json())

    def test_secretary_register_employee_respects_one_to_one(self, tmp_path: Path) -> None:
        from bandi.company.org_engine import OrgEmployee, OrgEngine
        from bandi.company.secretary import SecretaryService

        engine = OrgEngine()
        engine.register_role("qa-sec-role", "秘书岗位")
        engine.register_employee(
            OrgEmployee(employee_id="qa-sec-1", name="已有员工", role_id="qa-sec-role")
        )
        service = SecretaryService(base_dir=tmp_path / "secretary", org_engine=engine)
        result = service._apply_org_build_action(
            {"kind": "register_employee", "name": "第二人", "role_id": "qa-sec-role"}
        )
        assert "already staffed" in result
        assert len(engine.list_employees()) == 1

    def test_secretary_register_role_rejects_tombstone(self, tmp_path: Path) -> None:
        from bandi.company.employee_lifecycle import EmployeeTombstoneStore
        from bandi.company.org_engine import OrgEngine
        from bandi.company.secretary import SecretaryService

        EmployeeTombstoneStore().add(
            employee_id="qa-old-sec", role_id="qa-dead-role", name="旧秘书"
        )
        engine = OrgEngine()
        service = SecretaryService(base_dir=tmp_path / "secretary", org_engine=engine)
        result = service._apply_org_build_action(
            {"kind": "register_role", "role_id": "qa-dead-role", "name": "复活角色"}
        )
        assert "IDENTITY_RESERVED" in result
        assert engine.get_role("qa-dead-role") is None

    def test_auto_staffing_does_not_duplicate_occupied_role(self) -> None:
        from bandi.company.org_engine import OrgEmployee, OrgEngine
        from bandi.company.recruiter import Recruiter
        from bandi.company.staffing import ensure_role_staffed

        engine = OrgEngine()
        engine.register_role("qa-auto-role", "自动补员岗位")
        engine.register_employee(
            OrgEmployee(employee_id="qa-human", name="人工员工", role_id="qa-auto-role")
        )
        recruiter = Recruiter()
        outcome = ensure_role_staffed(engine, recruiter, "qa-auto-role")
        assert outcome == "ok"
        assert [item.employee_id for item in engine.list_employees()] == ["qa-human"]

    def test_cli_add_role_rejects_tombstoned_role(self) -> None:
        from typer.testing import CliRunner

        from bandi.cli import company as company_cli
        from bandi.company.employee_lifecycle import EmployeeTombstoneStore
        from bandi.company.org_engine import OrgEngine
        from bandi.company.recruiter import Recruiter

        EmployeeTombstoneStore().add(employee_id="qa-cli-old", role_id="qa-cli-role", name="旧角色")
        engine = OrgEngine()
        recruiter = Recruiter()
        with (
            patch.object(company_cli, "_engine", engine),
            patch.object(company_cli, "_recruiter", recruiter),
            patch.object(company_cli, "_save_org", lambda: None),
        ):
            result = CliRunner().invoke(company_cli.company_app, ["add-role", "qa-cli-role"])
        assert result.exit_code == 1
        assert "IDENTITY_RESERVED" in result.output
        assert engine.get_role("qa-cli-role") is None
