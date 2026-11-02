# 功能：Company 主链路端到端验证 — work_item 创建 → tick 流转 → 执行

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _build_dispatcher(tmp_path: Any, seat_executor: Any | None = None) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=seat_executor,
        config=DispatcherConfig(max_concurrent=3),
    )


def test_work_item_full_lifecycle(tmp_path: Any) -> None:
    """work_item（带 assignee）：tick 必须调度执行并离开 QUEUED（主链路真实流转）。"""
    disp = _build_dispatcher(tmp_path)
    item = create_work_item(
        title="整理报告",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    disp.store.save(item)

    asyncio.run(disp.tick())

    saved = disp.store.get(item.id)
    assert saved is not None
    assert saved.phase != Phase.QUEUED, f"tick 后 work_item 必须离开 QUEUED，实际 {saved.phase}"


def test_company_executor_receives_real_work_item(tmp_path: Any) -> None:
    """seat executor 必须收到真实 work_item（委托 AgentLoop 的输入）。"""
    dispatched: dict[str, Any] = {}

    class _RecordingExecutor:
        async def prepare_seat(self, runtime, work_item) -> None:
            pass

        async def run_turn(self, runtime, work_item) -> SeatResult:
            dispatched["title"] = work_item.title
            dispatched["id"] = work_item.id
            return SeatResult(success=True, output="done")

        async def checkpoint(self, runtime) -> dict[str, Any]:
            return {}

        async def interrupt(self, runtime) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    disp = _build_dispatcher(tmp_path, _RecordingExecutor())
    item = create_work_item(
        title="关键任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    disp.store.save(item)

    asyncio.run(disp.tick())
    assert dispatched.get("title") == "关键任务", "seat executor 必须收到真实 work_item"
    assert dispatched.get("id") == item.id


def test_engine_seat_executor_uses_real_agent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EngineSeatExecutor 生产实现必须委托 run_agent（真实 AgentLoop）。"""
    from pathlib import Path

    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.seat_executor import EngineSeatExecutor

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    called = {}

    import bandi.company.seat_executor as seat_mod

    async def fake_run_agent(user_input, mode="execute", session_id=None, **kwargs):
        called["input"] = user_input
        return "ok", "COMPLETED", 10, 1

    monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
    executor = EngineSeatExecutor()
    store = WorkItemStore(str(tmp_path / "company.db"))
    item = create_work_item(
        title="执行任务",
        turn_type=WorkItemTurnType.EXECUTE,
        description="生成季度报告",
    )
    item.metadata["prompt_contract"] = "遵守数据保密"
    store.save(item)
    runtime = CompanyRuntime(
        project_id="p1",
        session_scope_id="s1",
        role_id="worker",
        employee_id="e1",
    )
    result = asyncio.run(executor.run_turn(runtime, item))
    assert result.success is True
    assert called.get("input") is not None
    assert "执行任务" in called.get("input"), "必须把 work_item.title 传给 run_agent"
    assert "季度报告" in called.get("input"), "description 必须传给 run_agent"
    assert "数据保密" in called.get("input"), "metadata 契约字段必须传给 run_agent"


def test_seat_executor_injects_prompt_contract_assignment_context(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bandi.company.seat_executor as seat_mod
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.prompt_contract import make_prompt_contract

    called: dict[str, str] = {}

    async def fake_run_agent(user_input, mode="execute", session_id=None, **kwargs):
        called["input"] = user_input
        return "ok", "COMPLETED", 10, 1

    monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
    executor = seat_mod.EngineSeatExecutor()
    store = WorkItemStore(str(tmp_path / "company.db"))
    item = create_work_item(
        title="写市场简报",
        turn_type=WorkItemTurnType.EXECUTE,
        description="写市场简报",
    )
    item.metadata["prompt_contract"] = make_prompt_contract(
        task_brief="写市场简报",
        upstream_intent_summary="用户要做耳机市场进入分析",
        deliverables=["market_brief.md"],
        acceptance_criteria=["包含 3 个竞品"],
    )
    store.save(item)
    runtime = CompanyRuntime(
        project_id="p1",
        session_scope_id="s1",
        role_id="worker",
        employee_id="e1",
    )
    result = asyncio.run(executor.run_turn(runtime, item))
    assert result.success is True
    prompt = called["input"]
    assert "## Work Item Assignment Context" in prompt
    assert "Upstream Intent Summary" in prompt
    assert "用户要做耳机市场进入分析" in prompt
    assert "market_brief.md" in prompt
    assert "包含 3 个竞品" in prompt
