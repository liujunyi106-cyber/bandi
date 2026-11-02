# 功能：契约——plan 模式计划文件产物（审计：plan 无计划文件产物）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop, _persist_plan_file
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig
from tests.integration.helpers import MockProvider, RecordingBus


def _ledger(session: str = "sess-plan-1") -> TaskLedger:
    ledger = TaskLedger(config=TaskLedgerConfig(), session_id=session)
    ledger.apply_todos(
        [
            {"content": "调研竞品", "status": "in_progress"},
            {"content": "输出报告", "status": "pending"},
        ]
    )
    return ledger


def _ctx(mode: str, session: str = "sess-plan-1") -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="给我一个计划")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=RecordingBus(),
        meta={"session_id": session, "mode": mode},
    )
    ctx.raw_input = "给我一个计划"
    return ctx


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_plan_mode_persists_plan_file(home: Path) -> None:
    """契约（核心）：plan 模式终态落盘计划文件（台账渲染，用户可见产物）。"""
    ctx = _ctx("plan")
    ctx.meta["task_ledger"] = _ledger()

    _persist_plan_file(ctx)

    target = home / ".bandi" / "plans" / "plan-sess-pla.md"
    assert target.exists(), f"plan 模式必须产出计划文件（查找 {target}）"
    content = target.read_text(encoding="utf-8")
    assert "调研竞品" in content and "输出报告" in content
    assert "[>]" in content, "渲染须含状态标记"


def test_non_plan_mode_no_plan_file(home: Path) -> None:
    """契约（对照）：非 plan 模式不产出计划文件。"""
    ctx = _ctx("execute")
    ctx.meta["task_ledger"] = _ledger()

    _persist_plan_file(ctx)

    assert not (home / ".bandi" / "plans").exists(), "非 plan 模式不得产出计划文件"


def test_plan_mode_without_ledger_no_file(home: Path) -> None:
    """契约：plan 模式但无台账（未产生计划内容）不产出文件。"""
    ctx = _ctx("plan")

    _persist_plan_file(ctx)

    assert not (home / ".bandi" / "plans").exists()


def test_plan_file_overwrites_same_session(home: Path) -> None:
    """契约：同会话重复落盘覆盖同一文件（不堆积）。"""
    ctx = _ctx("plan")
    ctx.meta["task_ledger"] = _ledger()
    _persist_plan_file(ctx)

    ledger = _ledger()
    ledger.apply_todos([{"content": "更新后的任务", "status": "completed"}])
    ctx.meta["task_ledger"] = ledger
    _persist_plan_file(ctx)

    files = list((home / ".bandi" / "plans").glob("*.md"))
    assert len(files) == 1, f"同会话必须覆盖同一文件（实际 {files}）"
    content = files[0].read_text(encoding="utf-8")
    assert "更新后的任务" in content


@pytest.mark.asyncio
async def test_loop_run_wires_plan_file(home: Path) -> None:
    """契约（接线完整性）：AgentLoop.run 终态路径真实调用计划落盘（防 helper 孤儿）。"""
    provider = MockProvider()
    provider.add_response(LLMResponse("计划如上"))
    ctx = _ctx("plan", session="sess-plan-e2e")
    ctx.provider = provider
    ctx.config = AgentConfig(max_iterations=2)
    ctx.meta["task_ledger"] = _ledger("sess-plan-e2e")

    reason = await AgentLoop(max_iterations=2).run(ctx)

    assert reason in (TerminalReason.COMPLETED, TerminalReason.MAX_TURNS)
    target = home / ".bandi" / "plans" / "plan-sess-pla.md"
    assert target.exists(), "run 终态必须接线计划文件落盘"
