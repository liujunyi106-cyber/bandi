# 功能：CLI 链路验证 — 命令真实调用后端（非空转）

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_cli_entity_create_and_list(home: Any) -> None:
    """CLI entity 命令：三元组本体写入（类须已定义）并列表可见。"""
    from typer.testing import CliRunner

    from bandi.cli.app import app
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    seed = OntologyStore(open_ontology(str(home / ".bandi" / "data" / "ontology.db")))
    seed.define_class({"id": "Project", "label": "项目"}, "2026-01-01T00:00:00")
    seed.close()

    runner = CliRunner()
    r1 = runner.invoke(app, ["entity", "create", "Project", "测试项目"])
    assert r1.exit_code == 0, f"create 应成功，实际 exit={r1.exit_code}: {r1.output}"
    r2 = runner.invoke(app, ["entity", "list"])
    assert r2.exit_code == 0, f"list 应成功，实际 {r2.exit_code}"
    assert "测试项目" in r2.output, f"list 应包含创建的实体，实际 {r2.output}"


def test_cli_session_command(home: Any) -> None:
    """CLI session 命令必须真实可用。"""
    from typer.testing import CliRunner

    from bandi.cli.app import app

    runner = CliRunner()
    r = runner.invoke(app, ["session", "list"])
    assert r.exit_code == 0, f"session list 应成功，实际 {r.exit_code}: {r.output}"


def test_cli_health(home: Any) -> None:
    """CLI 健康/基础命令可执行。"""
    from typer.testing import CliRunner

    from bandi.cli.app import app

    runner = CliRunner()
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "init" in r.output or "session" in r.output or "entity" in r.output


def test_chat_list_tools_shows_real_tools(home: Any) -> None:
    """/tools 必须显示真实注册工具，不得恒为「无可用工具」。"""
    import asyncio
    from io import StringIO

    from rich.console import Console

    from bandi.cli.chat import ChatLoop

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    loop = ChatLoop(console=console)
    asyncio.run(loop._list_tools())
    output = buf.getvalue()
    assert "无可用工具" not in output, f"/tools 不得恒空: {output}"
    assert "read" in output or "file_write" in output or "grep" in output, (
        f"/tools 必须包含真实工具: {output}"
    )


def test_chat_execute_turn_forwards_project_employee(home: Any) -> None:
    """AR-20：CLI chat 必须把 project_id/employee_id 透传给 run_agent_turn——
    层级记忆（global/project/session/employee）在 CLI 链路真实生效，不得缺席。"""
    import asyncio
    from io import StringIO
    from types import SimpleNamespace
    from unittest.mock import patch

    from rich.console import Console

    from bandi.cli.chat import ChatLoop
    from bandi.cli.chat_controller import QueuedChatInput

    seen: dict[str, object] = {}

    async def fake_run_agent_turn(**kwargs: object) -> object:
        seen.update(kwargs)
        return SimpleNamespace(
            session_id="s1",
            terminal_reason="COMPLETED",
            total_tokens=0,
            turn_index=1,
        )

    buf = StringIO()
    loop = ChatLoop(
        console=Console(file=buf, force_terminal=False, width=120),
        project_id="p1",
        employee_id="e1",
    )
    with patch("bandi.core.agent_runner.run_agent_turn", new=fake_run_agent_turn):
        asyncio.run(loop._execute_turn(QueuedChatInput(text="hello")))

    assert seen.get("project_id") == "p1", f"project_id 必须透传: {seen}"
    assert seen.get("employee_id") == "e1", f"employee_id 必须透传: {seen}"
