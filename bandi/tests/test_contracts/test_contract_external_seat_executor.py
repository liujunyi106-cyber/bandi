# 功能：外部席位执行器契约——外部 Agent CLI 一次性执行 + 失败关闭 + token 如实标注
# （钉行为不钉实现；CLI 命令经 agent_commands 注入假命令，不依赖真实外部引擎）

from __future__ import annotations

import sys

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.work_item import WorkItemTurnType, create_work_item

FAKE_OK = (sys.executable, "-c", "print('external-done')")
FAKE_FAIL = (sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)")
FAKE_EMPTY = (sys.executable, "-c", "pass")
FAKE_HANG = (sys.executable, "-c", "import time; time.sleep(30)")


def _runtime() -> CompanyRuntime:
    return CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")


def _item(agent: str = "opencode", turn_type: WorkItemTurnType = WorkItemTurnType.EXECUTE):
    item = create_work_item(title="external task", turn_type=turn_type)
    item.runtime_metadata["assigned_external_agent"] = agent
    return item


@pytest.mark.asyncio
async def test_one_shot_execution_maps_stdout(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_OK})
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is True
    assert "external-done" in result.output
    assert result.metadata["external_agent"] == "opencode"
    assert result.metadata["terminal_reason"] == "COMPLETED"
    assert result.metadata["launch_command"]
    assert result.metadata["workspace"]


@pytest.mark.asyncio
async def test_unknown_agent_fails_closed(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path)
    result = await executor.run_turn(_runtime(), _item(agent="totally-unknown-agent"))
    assert result.success is False
    assert "totally-unknown-agent" in result.output


@pytest.mark.asyncio
async def test_missing_binary_fails_closed(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(
        workspace_root=tmp_path, agent_commands={"opencode": ("no-such-binary-xyz",)}
    )
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False
    assert result.metadata["terminal_reason"] == "FAILED"


@pytest.mark.asyncio
async def test_nonzero_exit_fails(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_FAIL})
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False
    assert result.metadata["returncode"] == 3


@pytest.mark.asyncio
async def test_timeout_kills_and_fails(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(
        workspace_root=tmp_path,
        agent_commands={"opencode": FAKE_HANG},
        command_timeout=2.0,
    )
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False
    assert result.metadata.get("timed_out") is True


@pytest.mark.asyncio
async def test_empty_output_fails(tmp_path):
    # 退出码 0 但零产出——不得算成功（外部一次性执行的产物就是 stdout）
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(
        workspace_root=tmp_path, agent_commands={"opencode": FAKE_EMPTY}
    )
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False
    assert "empty" in result.output.lower()


@pytest.mark.asyncio
async def test_token_accounting_honest(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_OK})
    result = await executor.run_turn(_runtime(), _item())
    assert result.metadata["total_tokens"] is None
    assert result.metadata["token_accounting"]


@pytest.mark.asyncio
async def test_output_contract_violation_blocks(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_OK})
    result = await executor.run_turn(_runtime(), _item(turn_type=WorkItemTurnType.REPORT))
    assert result.success is False
    assert result.metadata["output_contract_violation"]["missing_keys"] == ["summary"]


@pytest.mark.asyncio
async def test_output_contract_validator_crash_blocks(tmp_path, monkeypatch):
    # 破坏者：契约校验器抛异常 → fail-closed 阻断（不得静默放行外部产出）
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_OK})

    def crasher(_item):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr("bandi.company.external_seat_executor.validate_work_item_output", crasher)
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False, "校验器异常必须 fail-closed 阻断"
    violation = result.metadata.get("output_contract_violation")
    assert violation is not None
    assert any("校验器" in e for e in violation["format_errors"]), violation


@pytest.mark.asyncio
async def test_protocol_compliance(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor
    from bandi.company.seat_executor import SeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path)
    runtime = _runtime()
    item = _item()
    await executor.prepare_seat(runtime, item)
    cp = await executor.checkpoint(runtime)
    assert cp["session"] == runtime.member_session_id()
    await executor.interrupt(runtime)
    await executor.shutdown()
    # 协议兼容：可作为 SeatExecutor 注入 CompanyDispatcher
    assert isinstance(executor, SeatExecutor)


import asyncio  # noqa: E402


def _process_alive(pid: int) -> bool:
    """Windows 下探测 pid 是否存活（tasklist 过滤）。

    bytes 模式 + errors=replace——中文 Windows tasklist 输出非 UTF-8
    text=True 解码异常会令 stdout 变 None。
    """
    import subprocess as sp

    out = sp.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, check=False).stdout
    return str(pid) in (out or b"").decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_cancellation_kills_child_process(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(
        workspace_root=tmp_path,
        agent_commands={"opencode": FAKE_HANG},
    )
    runtime = _runtime()
    task = asyncio.create_task(executor.run_turn(runtime, _item()))
    for _ in range(50):
        await asyncio.sleep(0.1)
        if executor._active_procs:
            break
    assert executor._active_procs, "spawn 后活动进程必须登记"
    proc = next(iter(executor._active_procs.values()))
    pid = proc.pid

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor._active_procs == {}, "取消后活动登记必须清空"
    for _ in range(20):
        if not _process_alive(pid):
            break
        await asyncio.sleep(0.2)
    assert not _process_alive(pid), f"取消后子进程 {pid} 必须已被杀"


@pytest.mark.asyncio
async def test_timeout_uses_tree_kill_and_clears_registry(tmp_path, monkeypatch):
    from bandi.company import external_seat_executor as ese
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    calls: list[int] = []
    monkeypatch.setattr(ese, "kill_process_tree", lambda proc: calls.append(proc.pid))
    executor = ExternalSeatExecutor(
        workspace_root=tmp_path,
        agent_commands={"opencode": FAKE_HANG},
        command_timeout=1.0,
    )
    result = await executor.run_turn(_runtime(), _item())
    assert result.success is False and result.metadata.get("timed_out") is True
    assert calls, "超时路径必须走树杀原语（proc.kill 单进程不够）"
    assert executor._active_procs == {}


@pytest.mark.asyncio
async def test_interrupt_kills_active_process(tmp_path, monkeypatch):
    from bandi.company import external_seat_executor as ese
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    calls: list[int] = []
    monkeypatch.setattr(ese, "kill_process_tree", lambda proc: calls.append(proc.pid))
    executor = ExternalSeatExecutor(workspace_root=tmp_path)
    runtime = _runtime()
    await executor.prepare_seat(runtime, _item())
    hang = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    executor._active_procs[runtime.member_session_id()] = hang
    await executor.interrupt(runtime)
    assert calls == [hang.pid], "interrupt 必须树杀该 session 活动进程"
    assert runtime.member_session_id() not in executor._active_procs
    try:
        hang.kill()
        await hang.wait()
    except ProcessLookupError:
        pass


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows CreateProcess 32767 上限")
async def test_prompt_exceeding_argv_limit_fails_closed_with_clear_error(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=tmp_path, agent_commands={"opencode": FAKE_OK})
    item = create_work_item(
        title="long task",
        turn_type=WorkItemTurnType.EXECUTE,
        description="x" * 40000,
    )
    item.runtime_metadata["assigned_external_agent"] = "opencode"
    result = await executor.run_turn(_runtime(), item)
    assert result.success is False
    assert "argv" in result.output.lower()
    assert result.metadata["terminal_reason"] == "FAILED"


@pytest.mark.asyncio
async def test_workspace_path_traversal_fails_closed(tmp_path):
    from bandi.company.external_seat_executor import ExternalSeatExecutor

    executor = ExternalSeatExecutor(workspace_root=None, agent_commands={"opencode": FAKE_OK})
    runtime = CompanyRuntime(
        project_id="../evil", session_scope_id="s", role_id="r", employee_id="e"
    )
    result = await executor.run_turn(runtime, _item())
    assert result.success is False
    assert "project_id" in result.output
