from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandi.cli.company import company_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_engine(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bandi.cli.company as mcc
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import EngineSeatExecutor

    # Path.home patch + env 双保险：_dispatcher/_reorg 的默认路径（Path.home()）与
    # 组件 expanduser("~")（HOME/USERPROFILE）都不落真实用户目录
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    mcc._engine = OrgEngine()
    mcc._recruiter = Recruiter()
    mcc._executor = EngineSeatExecutor()
    # ~/.bandi/company/org.json，测试角色污染用户组织配置
    mcc._org_db_path = str(tmp_path / "org.json")


class TestCompanyCli:
    def test_init(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "2"])
        assert result.exit_code == 0
        assert "CEO" in result.stdout

    def test_roles_shows_builtins(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["roles"])
        assert result.exit_code == 0
        assert "CEO" in result.stdout
        assert "Engineer" in result.stdout

    def test_roles_after_init(self, runner: CliRunner) -> None:
        runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "1"])
        result = runner.invoke(company_app, ["roles"])
        assert result.exit_code == 0
        assert "Boss" in result.stdout

    def test_recruit(self, runner: CliRunner) -> None:
        runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "1"])
        result = runner.invoke(company_app, ["recruit", "ceo"])
        assert result.exit_code == 0

    def test_recruit_invalid_role(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["recruit", "ghost"])
        assert result.exit_code == 0

    def test_status(self, runner: CliRunner) -> None:
        runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "2"])
        result = runner.invoke(company_app, ["status"])
        assert result.exit_code == 0
        assert "Available" in result.stdout

    def test_add_role(self, runner: CliRunner) -> None:
        result = runner.invoke(
            company_app,
            ["add-role", "designer", "--name", "Designer", "--reports-to", "ceo"],
        )
        assert result.exit_code == 0
        assert "designer" in result.stdout
        listed = runner.invoke(company_app, ["roles"])
        assert "Designer" in listed.stdout

    def test_add_role_can_spawn(self, runner: CliRunner) -> None:
        import bandi.cli.company as mcc

        result = runner.invoke(
            company_app,
            ["add-role", "manager", "--name", "Mgr", "--can-spawn", "ui", "--can-spawn", "ux"],
        )
        assert result.exit_code == 0
        assert mcc._engine.get_role("manager") is not None
        assert mcc._engine.get_role("manager").can_spawn == ["ui", "ux"]

    def test_add_role_self_report_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["add-role", "x", "--name", "X", "--reports-to", "x"])
        assert result.exit_code != 0
        assert "report to itself" in result.stdout

    def test_add_role_unknown_reports_to_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(
            company_app, ["add-role", "y", "--name", "Y", "--reports-to", "ghost"]
        )
        assert result.exit_code != 0
        assert "Unknown reports_to" in result.stdout

    def test_add_employee(self, runner: CliRunner) -> None:
        import bandi.cli.company as mcc

        result = runner.invoke(
            company_app, ["add-employee", "e1", "--name", "Alice", "--role", "engineer"]
        )
        assert result.exit_code == 0
        assert "e1" in result.stdout
        employee = mcc._engine.get_employee("e1")
        assert employee is not None
        assert employee.name == "Alice"
        assert employee.role_id == "engineer"

    def test_add_employee_unknown_role_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(
            company_app, ["add-employee", "e2", "--name", "Bob", "--role", "ghost"]
        )
        assert result.exit_code != 0
        assert "Unknown role" in result.stdout

    def test_init_idempotent(self, runner: CliRunner, tmp_path) -> None:
        """
        不含 worker_N → 每次 init 累积 + 持久化污染）。"""
        import json

        import bandi.cli.company as mcc

        runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "2"])
        runner.invoke(company_app, ["init", "--ceo-name", "Boss", "--worker-count", "3"])
        workers = [r.role_id for r in mcc._engine.list_agents() if r.role_id.startswith("worker_")]
        assert workers == ["worker_1", "worker_2", "worker_3"], "init 必须幂等：worker 角色不得累积"
        payload = json.loads((tmp_path / "org.json").read_text(encoding="utf-8"))
        role_ids = [r["id"] for r in payload["roles"]]
        assert not any(rid.startswith("worker_") for rid in role_ids), "init 产物不得持久化"

    def test_org_persisted_across_processes(self, runner: CliRunner) -> None:
        """
        init/add-role 后进程退出即丢，tick 的新 dispatcher 看不到自定义角色。
        （org.json 路径已由 autouse fixture 隔离到 tmp_path。）"""
        import bandi.cli.company as mcc

        runner.invoke(company_app, ["add-role", "designer", "--name", "Designer"])
        runner.invoke(company_app, ["add-employee", "e9", "--name", "Eve", "--role", "designer"])
        # 模拟新进程：重建 engine/recruiter 并从持久化恢复
        from bandi.company.org_engine import OrgEngine
        from bandi.company.recruiter import Recruiter

        mcc._engine = OrgEngine()
        mcc._recruiter = Recruiter()
        mcc._load_persisted_org()
        assert mcc._engine.get_role("designer") is not None, "角色必须从持久化恢复"
        emp = mcc._engine.get_employee("e9")
        assert emp is not None and emp.role_id == "designer", "员工必须从持久化恢复"


class TestCollabDispatch:
    def _make_env(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("BANDI_COMMS_FROM", "worker")
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("BANDI_COMMS_PROJECT", "p1")
        monkeypatch.setenv("BANDI_COMMS_SESSION", "s1")

    def test_collab_dispatch_send_dm(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._make_env(runner, monkeypatch, tmp_path)
        result = runner.invoke(
            company_app,
            [
                "collab-dispatch",
                "send_dm",
                "--args-json",
                '{"to_agent":"bob","subject":"hi","body":"hello"}',
            ],
        )
        assert result.exit_code == 1, f"exit={result.exit_code} out={result.output!r}"
        data = json.loads(result.output.strip().splitlines()[-1])
        assert "permission denied" in data.get("error", "").lower()

    def test_collab_dispatch_unknown_tool(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._make_env(runner, monkeypatch, tmp_path)
        result = runner.invoke(company_app, ["collab-dispatch", "nonexistent"])
        assert result.exit_code == 1, f"exit={result.exit_code} out={result.output!r}"
        data = json.loads(result.output.strip().splitlines()[-1])
        assert "error" in data
        assert "unknown tool" in data["error"].lower()

    def test_collab_dispatch_no_role_fails(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BANDI_COMMS_FROM", raising=False)
        result = runner.invoke(company_app, ["collab-dispatch", "send_dm", "--args-json", "{}"])
        assert result.exit_code != 0
        assert "BANDI_COMMS_FROM" in result.output or "active role" in result.output.lower()


class TestPreflight:
    @staticmethod
    def _extract_json(output: str):
        # 防御：safe-delete shim 等环境日志可能混进 stdout（高负载下时隐时现）
        # 从首个 '[' 起解析，避免环境伪失败
        return json.loads(output[output.index("[") :])

    def test_preflight_runs(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["preflight"])
        assert result.exit_code == 0, f"exit={result.exit_code} out={result.output!r}"
        data = self._extract_json(result.output)
        assert isinstance(data, list)
        assert len(data) > 0
        agents = {d["agent"] for d in data}
        assert "opencode" in agents or len(agents) >= 1

    def test_preflight_json_output_valid(self, runner: CliRunner) -> None:
        result = runner.invoke(company_app, ["preflight"])
        assert result.exit_code == 0
        data = self._extract_json(result.output)
        for entry in data:
            assert "agent" in entry
            assert "ok" in entry
            assert "available" in entry
            assert "write_checks" in entry
