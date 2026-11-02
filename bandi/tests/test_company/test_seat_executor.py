from __future__ import annotations

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.work_item import WorkItemTurnType, create_work_item


@pytest.mark.asyncio
async def test_prepare_and_run():
    executor = EngineSeatExecutor()
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    item = create_work_item(title="test", turn_type=WorkItemTurnType.EXECUTE)
    await executor.prepare_seat(runtime, item)
    result = await executor.run_turn(runtime, item)
    assert isinstance(result, SeatResult)


@pytest.mark.asyncio
async def test_run_turn_verified_is_success(monkeypatch):
    from bandi.company.seat_executor import EngineSeatExecutor, SeatResult

    async def fake_run_agent(
        *,
        user_input,
        mode,
        session_id,
        work_item=None,
        project_id=None,
        employee_id=None,
        group_conversation_id=None,
        dm_conversation_id=None,
        project_root=None,
        communication=None,
        agent_role="user",
        company_runtime=None,
        role_tool_tags=None,
        role_fs_scopes=None,
        manager_capable=True,
        company_store=None,
        company_thread_manager=None,
        company_org_engine=None,
        provider_config=None,
        abort_controller=None,
        user_id=None,
        stream_handler=None,
        user_interaction=None,
        relax_workspace_asks=False,
        mcp_allowed_servers=None,
        approval_capture=None,
    ):
        assert work_item is not None, "run_agent 必须透传 work_item，ownership 守卫才有数据源"
        assert user_id, "run_agent 必须透传 user_id（C3：执行面身份归因，缺省回落本地默认用户）"
        assert project_id == "p" and employee_id == "e", "run_agent 必须透传 runtime 维度"
        assert agent_role == "r", "run_agent 必须透传 runtime.role_id"
        assert isinstance(approval_capture, dict), "员工链必须透传审批锚点回填容器"
        assert (
            company_store is None and company_thread_manager is None and company_org_engine is None
        ), "未装配 store/thread_manager/org_engine 时 spawn deps 必须为 None（fail-closed 不注入）"
        assert relax_workspace_asks is True, (
            "员工执行链必须放宽工作区写类工具询问（边界由 fs/shell 守卫与 deny 规则兜底）"
        )
        return "verified output", "VERIFIED", 100, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    executor = EngineSeatExecutor()
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    item = create_work_item(title="test", turn_type=WorkItemTurnType.EXECUTE)
    result = await executor.run_turn(runtime, item)
    assert isinstance(result, SeatResult)
    assert result.success is True
    assert result.metadata["terminal_reason"] == "VERIFIED"


@pytest.mark.asyncio
async def test_checkpoint():
    executor = EngineSeatExecutor()
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    cp = await executor.checkpoint(runtime)
    assert "session" in cp
    assert cp["queue_size"] == 0


@pytest.mark.asyncio
async def test_shutdown():
    executor = EngineSeatExecutor()
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    item = create_work_item(title="cleanup", turn_type=WorkItemTurnType.EXECUTE)
    await executor.prepare_seat(runtime, item)
    await executor.shutdown()
