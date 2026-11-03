# 功能：CLI 层单元测试 — ChatLoop 命令 / UserInteraction / render


import pytest

from bandi.cli.chat import ChatLoop
from bandi.cli.ui_state import UIStateStore


@pytest.fixture
def loop(tmp_path) -> ChatLoop:
    return ChatLoop(ui_store=UIStateStore(str(tmp_path / "ui_state.db")))


class TestChatLoop:
    def test_initial_mode_execute(self, loop: ChatLoop) -> None:
        assert loop._state.mode == "execute"

    @pytest.mark.asyncio
    async def test_handle_command_quit(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/quit")
        assert result is True

    @pytest.mark.asyncio
    async def test_handle_command_exit(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/exit")
        assert result is True

    @pytest.mark.asyncio
    async def test_handle_command_help(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/help")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_command_plan(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/plan")
        assert result is False
        assert loop._state.mode == "plan"

    @pytest.mark.asyncio
    async def test_handle_command_ask_removed(self, loop: ChatLoop) -> None:
        """双模式化：/ask 命令已移除，作为未知命令处理。"""
        result = await loop._handle_command("/ask")
        assert result is False
        assert loop._state.mode != "ask"

    @pytest.mark.asyncio
    async def test_handle_command_execute(self, loop: ChatLoop) -> None:
        loop._state.mode = "plan"
        result = await loop._handle_command("/execute")
        assert result is False
        assert loop._state.mode == "execute"

    @pytest.mark.asyncio
    async def test_handle_command_mode(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/mode")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_unknown_command(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/unknown")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_empty_slash(self, loop: ChatLoop) -> None:
        result = await loop._handle_command("/")
        assert result is False


def test_audit_help() -> None:
    from typer.testing import CliRunner

    from bandi.cli.app import app

    runner = CliRunner()
    result = runner.invoke(app, ["audit", "--help"])
    assert result.exit_code == 0
    assert "审计" in result.stdout


def test_company_reorg_help() -> None:
    from typer.testing import CliRunner

    from bandi.cli.app import app

    runner = CliRunner()
    # 帮助输出的宽度由终端决定，显式给足宽度，避免窄终端把选项截断
    result = runner.invoke(app, ["company", "reorg", "--help"], terminal_width=200)
    assert result.exit_code == 0
    assert "--replace" in result.stdout


def test_company_reorg_noop() -> None:
    from typer.testing import CliRunner

    from bandi.cli import company as company_cli

    runner = CliRunner()
    result = runner.invoke(company_cli.company_app, ["reorg"])
    assert result.exit_code == 0
    assert "Nothing to do" in result.stdout


def test_company_reorg_replace_migrates_employee(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from bandi.cli import company as company_cli
    from bandi.company.org_engine import OrgEmployee, OrgEngine
    from bandi.company.reorg_manager import ReorgManager
    from bandi.company.store import WorkItemStore

    engine = OrgEngine()
    engine.register_role("dev", "Dev", reports_to="ceo")
    engine.register_employee(OrgEmployee(employee_id="e1", name="E1", role_id="dev"))
    manager = ReorgManager(store=WorkItemStore(str(tmp_path / "cli.db")), org_engine=engine)
    monkeypatch.setattr(company_cli, "_engine", engine)
    monkeypatch.setattr(company_cli, "_reorg", lambda: manager)

    runner = CliRunner()
    result = runner.invoke(
        company_cli.company_app,
        ["reorg", "--replace", "dev=senior_dev", "--summary", "cli reorg"],
    )
    assert result.exit_code == 0
    assert "Applied reorg" in result.stdout
    assert engine.get_employee("e1").role_id == "senior_dev"


def test_company_tick_drives_schedule(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from bandi.cli import company as company_cli
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes work items")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=2),
    )
    item = create_work_item(
        title="CLI 任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    store.save(item)
    monkeypatch.setattr(company_cli, "_dispatcher", lambda: (dispatcher, store))

    runner = CliRunner()
    result = runner.invoke(company_cli.company_app, ["tick"])
    assert result.exit_code == 0
    assert "processed" in result.stdout
    assert "tick" in result.stdout


def test_company_kanban_prints_columns(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from bandi.cli import company as company_cli
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes work items")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=2),
    )
    item = create_work_item(
        title="看板任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    store.save(item)
    monkeypatch.setattr(company_cli, "_dispatcher", lambda: (dispatcher, store))

    runner = CliRunner()
    result = runner.invoke(company_cli.company_app, ["kanban"])
    assert result.exit_code == 0
    assert "Column" in result.stdout
    assert "todo" in result.stdout


def test_company_kanban_empty(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from bandi.cli import company as company_cli
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes work items")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=2),
    )
    monkeypatch.setattr(company_cli, "_dispatcher", lambda: (dispatcher, store))

    runner = CliRunner()
    result = runner.invoke(company_cli.company_app, ["kanban"])
    assert result.exit_code == 0
    assert "No work items" in result.stdout
