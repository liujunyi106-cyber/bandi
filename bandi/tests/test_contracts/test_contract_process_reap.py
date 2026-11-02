"""
契约族：进程树回收原语 reap_process（同族超时兜底，免疫）。

背景：bash/git_ops/python_exec 三模块在取消/超时路径都是
kill_process_tree → await proc.wait()。bash 有 _wait_reap（2s 超时），其余
两模块裸 wait——树杀 best-effort 失败（进程残留）时 wait 永挂，阻塞
CancelledError/TimeoutError 传播（取消路径卡死）。修复：公共原语
`subprocess_tree.reap_process`（wait_for 2s + 吞回收异常），三处委托。

本契约钉：
- reap_process：wait 永不返回时必须在超时内返回且不抛（不阻塞原异常链）
- git_ops 超时路径：communicate 超时 + wait 永挂 → TimeoutError 仍正常传播

行为族（生产调用路径）：
- `tools/subprocess_tree.py::reap_process`（公共原语）
- `tools/git_ops.py::_run_git` TimeoutError 分支
- `tools/bash.py::_wait_reap` / `tools/python_exec.py::_wait_reap`（委托同族，由既有测试钉住）

断言「最终产物」：挂起的回收不阻断异常传播（超时返回而非永挂）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.tools.subprocess_tree import reap_process


@pytest.mark.asyncio
async def test_contract_reap_process_hang_returns_within_timeout() -> None:
    """公共原语：wait 永不返回（树杀失败残留）时 reap 在 ~2s 超时内返回且不抛。"""
    proc = MagicMock()
    hang = asyncio.Event()
    proc.wait = AsyncMock(side_effect=hang.wait)  # 永不 set
    # 3s 护栏：reap 自身 2s 超时兜底——护栏内完成即证明不永挂
    await asyncio.wait_for(reap_process(proc), timeout=3.0)
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_contract_reap_process_normal_wait_returns() -> None:
    """公共原语：进程正常退出（wait 立即返回）时 reap 直接通过。"""
    proc = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    await reap_process(proc)


@pytest.mark.asyncio
async def test_contract_git_ops_timeout_hang_does_not_block() -> None:
    """git_ops 超时路径同族：communicate 超时 + 回收 wait 永挂 → TimeoutError 仍传播。"""
    from bandi.tools.git_ops import _run_git

    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError())
    proc.kill = MagicMock()
    hang = asyncio.Event()
    proc.wait = AsyncMock(side_effect=hang.wait)
    with (
        patch(
            "bandi.tools.git_ops.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
        patch("bandi.tools.git_ops.kill_process_tree") as kill_spy,
        # _run_git 函数内 from fs_tool import workspace_root——须 patch fs_tool 模块层
        patch("bandi.tools.fs_tool.workspace_root", return_value=Path(".")),
        patch("bandi.tools.fs_tool.resolve_within_workspace", return_value=Path(".")),
    ):
        with pytest.raises(TimeoutError):
            # 4s 护栏：若回收挂起阻塞，这里会 asyncio.TimeoutError（护栏）而非 git 超时语义
            await asyncio.wait_for(_run_git(["status"], timeout=1), timeout=4.0)
    assert kill_spy.called, "超时必须杀进程树"
