"""
契约族：本地长时工具 cancellable 接线（中断即时生效）。

背景：executor abort 竞速机制（executor._run_and_record cancellable 分支）与
bash/python_exec 的 CancelledError 进程树清理路径均已存在，但本地工具零声明
cancellable=True——abort 只对 MCP 工具生效，用户中断后本地长任务只能等内部
超时，无法即时取消。本契约钉：
- 本地长时子进程工具（bash/python_exec）必须声明 cancellable=True（族级覆盖全部成员）
- cancellable 声明的本地工具执行中收到 abort：竞速取消 + 子进程树被杀
  （CancelledError 路径走 _kill_process_tree，不残留孤儿进程）
- 非 cancellable 本地工具（如 file_edit）不受影响（保持等待语义）

行为族（生产调用路径）：
- `tools/bash.py::BashTool.execute`（CancelledError 分支树杀）
- `tools/python_exec.py::PythonExecTool.execute`（CancelledError 分支树杀）
- `tools/executor.py::_run_and_record`（cancellable abort 竞速）
- `agent/factory.py::build_tool_registry`（生产装配实例）

断言「最终产物」：abort 后返回 cancelled 失败结果且进程树清理被调用。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.core.abort_controller import AbortController
from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


def _probe_executor(tool: object) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register_instance(tool)  # type: ignore[arg-type]
    return ToolExecutor(registry=reg, abort_controller=AbortController())


def _local_long_tools() -> list[tuple[type, str]]:
    from bandi.tools.bash import BashTool
    from bandi.tools.python_exec import PythonExecTool

    return [(BashTool, "bash"), (PythonExecTool, "python_exec")]


@pytest.mark.parametrize(("tool_cls", "name"), _local_long_tools())
def test_contract_local_long_tools_declare_cancellable(tool_cls: type, name: str) -> None:
    """族级 DoD-2：全部本地长时子进程工具必须声明 cancellable=True（生产装配实例）。"""
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry()
    instance = registry.get(name)
    assert instance is not None, f"生产装配缺少工具 {name}"
    assert getattr(instance, "cancellable", False) is True, (
        f"本地长时工具 {name} 未声明 cancellable=True——用户中断无法即时取消"
    )


@pytest.mark.asyncio
async def test_contract_local_bash_abort_during_exec_cancels_and_kills_tree() -> None:
    """破坏者视角：bash 长命令执行中收到 abort——竞速取消 + 进程树被杀，返回 cancelled。"""
    from bandi.tools.bash import BashTool
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    tool = BashTool()
    assert tool.cancellable is True

    class _AllowAll:
        """审批通道可用（ask_permission 恒真）——绕过 headless 审批闸直抵执行。"""

        async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
            return True

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    async def _hang_read(n: int = -1) -> bytes:
        await asyncio.sleep(60)
        return b""

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = _hang_read
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = _hang_read

    kill_spy = MagicMock()
    reap_spy = MagicMock()

    controller = AbortController()
    reg = ToolRegistry()
    reg.register_instance(tool)
    ex = ToolExecutor(
        registry=reg,
        abort_controller=controller,
        permissions_pipeline=PermissionsPipeline(user_interaction=_AllowAll()),
    )

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(BashTool, "_kill_process_tree", kill_spy),
        patch.object(BashTool, "_wait_reap", reap_spy),
    ):
        task = asyncio.create_task(
            ex.execute([ToolCall(id="b1", name="bash", arguments={"command": "sleep 60"})])
        )
        await asyncio.sleep(0.2)  # 让工具真正开始执行（进入 communicate）
        controller.abort("user_interrupt")
        (result,) = await task

    assert result.success is False, "abort 后工具必须返回失败"
    assert "abort" in (result.error or "").lower() or result.should_abort, (
        f"错误语义不符: {result.error!r}"
    )
    assert kill_spy.called, "abort 取消路径必须杀进程树（防孤儿进程残留）"


@pytest.mark.asyncio
async def test_contract_non_cancellable_not_started_after_abort() -> None:
    """（审计，用户点名）：abort 已触发时**任何工具**（含
    非 cancellable）不得启动——原「等待语义」仅保留给已进入执行中的调用
    （见下条用例）。"""
    from bandi.tools.base import BaseTool, ToolCategory

    class _StubbornLocalTool(BaseTool):
        name = "local_stubborn"
        description = "本地不可取消探针"
        category = ToolCategory.UTIL
        cancellable = False

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def execute(self, params, context=None) -> str:  # type: ignore[override]
            self.calls += 1
            return "done"

    tool = _StubbornLocalTool()
    assert tool.cancellable is False
    controller = AbortController()
    controller.abort("user_interrupt")
    reg = ToolRegistry()
    reg.register_instance(tool)
    ex = ToolExecutor(registry=reg, abort_controller=controller)
    (result,) = await ex.execute([ToolCall(id="f1", name="local_stubborn", arguments={})])
    assert result.success is False, "abort 后非 cancellable 工具也不得启动"
    assert "aborted" in (result.error or "").lower(), f"实际 {result.error!r}"
    assert tool.calls == 0, "工具本体不得被调用"


@pytest.mark.asyncio
async def test_contract_running_non_cancellable_not_interrupted() -> None:
    """保留语义（对照组）：已进入执行中的非 cancellable 工具不因 abort 被打断
    （等待语义——不可取消=执行期不强制终止，防止半途副作用）。"""
    from bandi.tools.base import BaseTool, ToolCategory

    class _SlowStubborn(BaseTool):
        name = "local_slow_stubborn"
        description = "执行中不可取消探针"
        category = ToolCategory.UTIL
        cancellable = False

        def __init__(self) -> None:
            super().__init__()
            self.finished = False

        async def execute(self, params, context=None) -> str:  # type: ignore[override]
            await asyncio.sleep(0.15)
            self.finished = True
            return "done"

    tool = _SlowStubborn()
    controller = AbortController()
    reg = ToolRegistry()
    reg.register_instance(tool)
    ex = ToolExecutor(registry=reg, abort_controller=controller)

    async def _abort_later() -> None:
        await asyncio.sleep(0.05)
        controller.abort("user_interrupt")

    results, _ = await asyncio.gather(
        ex.execute([ToolCall(id="f2", name="local_slow_stubborn", arguments={})]),
        _abort_later(),
    )
    (result,) = results
    assert tool.finished is True, "执行中的非 cancellable 工具不得被打断（等待语义）"
    assert result.success is True
