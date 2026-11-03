from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.tools.python_exec import PythonExecTool


@pytest.fixture
def tool() -> PythonExecTool:
    return PythonExecTool()


class TestPythonExecTool:
    @pytest.mark.asyncio
    async def test_simple_print(self, tool: PythonExecTool) -> None:
        result = await tool.execute({"code": "print('hello from memo')"})
        assert result["ok"] is True
        assert "hello from memo" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_syntax_error(self, tool: PythonExecTool) -> None:
        result = await tool.execute({"code": "print('hello'"})
        assert result["ok"] is False
        assert result["exit_code"] != 0
        assert "stderr" in result

    @pytest.mark.asyncio
    async def test_computation(self, tool: PythonExecTool) -> None:
        result = await tool.execute({"code": "print(sum(i * i for i in range(100)))"})
        assert result["ok"] is True
        assert "328350" in result["stdout"]

    @pytest.mark.asyncio
    async def test_timeout(self, tool: PythonExecTool) -> None:
        result = await tool.execute({"code": "import time; time.sleep(10)", "timeout": 1})
        assert result["ok"] is False
        assert "timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_no_code_error(self, tool: PythonExecTool) -> None:
        with pytest.raises(KeyError):
            await tool.execute({})

    @pytest.mark.asyncio
    async def test_risk_level(self, tool: PythonExecTool) -> None:
        assert tool.risk_level == "high"
        assert tool.is_concurrency_safe is False

    @pytest.mark.asyncio
    async def test_cancelled_error_kills_process_tree(self, tool: PythonExecTool) -> None:
        """+ 同族：外部取消（CancelledError）必须杀整棵进程树，不得留下孤儿进程。"""

        class _CancelledStream:
            async def read(self, n: int) -> bytes:
                raise asyncio.CancelledError()

        proc = MagicMock()
        proc.stdout = _CancelledStream()
        proc.stderr = _CancelledStream()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        with patch(
            "bandi.tools.python_exec.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with patch("bandi.tools.python_exec.kill_process_tree") as mock_tree:
                with pytest.raises(asyncio.CancelledError):
                    await tool.execute({"code": "print('x')", "timeout": 1})
        mock_tree.assert_called_once_with(proc), "取消时必须杀整棵进程树"
        proc.kill.assert_not_called(), "禁止回退到只杀首进程"

    @pytest.mark.asyncio
    async def test_cancel_reap_wait_hang_does_not_block(self, tool: PythonExecTool) -> None:
        """同族（bash _wait_reap 2s 超时）：进程树杀失败（wait 永不返回）时
        取消路径不得被回收挂起阻塞——CancelledError 必须继续传播。"""

        class _CancelledStream:
            async def read(self, n: int) -> bytes:
                raise asyncio.CancelledError()

        proc = MagicMock()
        proc.stdout = _CancelledStream()
        proc.stderr = _CancelledStream()
        proc.kill = MagicMock()
        hang = asyncio.Event()
        proc.wait = AsyncMock(side_effect=hang.wait)  # wait 永不 set——模拟树杀失败残留
        with patch(
            "bandi.tools.python_exec.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with patch("bandi.tools.python_exec.kill_process_tree"):
                with pytest.raises(asyncio.CancelledError):
                    # 5s 外层护栏：若回收挂起阻塞传播，这里会 TimeoutError 而非 CancelledError
                    await asyncio.wait_for(
                        tool.execute({"code": "print('x')", "timeout": 1}), timeout=5
                    )
