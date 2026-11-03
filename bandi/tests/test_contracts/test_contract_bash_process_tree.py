"""
契约族：bash 进程树生命周期。

背景：BashTool.execute 超时/取消时只 `proc.kill()` 杀首进程，子进程树
（powershell 起的 Start-Process、管道、后台任务等）残留为孤儿进程
反复触发会堆积资源并留下不可控的执行残留。

本契约钉的行为族（全部生产调用路径）：
- BashTool._spawn_kwargs（spawn 隔离前置：win32 独立进程组 / posix 新会话）
- BashTool.execute 生产链路透传隔离参数（每 spawn 必隔离）
- BashTool.execute 超时路径：树杀整棵子树而非首进程
- BashTool.execute 取消路径：树杀整棵子树而非首进程
- BashTool._kill_process_tree 平台原语（win32 taskkill /T /F / posix killpg）
- 树杀 best-effort：进程已死/原语失败不得吞掉正常异常流

断言「最终产物」：真实 spawn 一棵 python 进程树后执行树杀，root 与后代
进程都必须实际退出（后代 PID 轮询不复存在）。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.tools.bash import BashTool

_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
_SIGKILL = getattr(signal, "SIGKILL", 9)


class _EmptyStream:
    # 功能：空输出流（read 立即 EOF）
    async def read(self, n: int = -1) -> bytes:
        return b""


def _make_stream_proc(pid: int = 4321) -> MagicMock:
    # 功能：构造读循环形态的子进程 mock（空输出流 + returncode + 可等待 wait）
    proc = MagicMock()
    proc.stdout = _EmptyStream()
    proc.stderr = _EmptyStream()
    proc.returncode = 0
    proc.pid = pid
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


def _try_spawn_kwargs(platform: str) -> dict:
    with patch("bandi.tools.subprocess_tree.sys.platform", platform):
        return BashTool._spawn_kwargs()


def test_contract_spawn_kwargs_isolate_process_tree() -> None:
    """spawn 隔离：win32 必须带 CREATE_NEW_PROCESS_GROUP，posix 必须新会话。"""
    win = _try_spawn_kwargs("win32")
    assert win.get("creationflags", 0) & _CREATE_NEW_PROCESS_GROUP, (
        f"win32 必须使用独立进程组，实际 {win!r}"
    )
    posix = _try_spawn_kwargs("linux")
    assert posix.get("start_new_session") is True, f"posix 必须新会话，实际 {posix!r}"
    assert "creationflags" not in posix, "posix 不得带 Windows-only 参数"


@pytest.mark.asyncio
async def test_contract_execute_forwards_spawn_isolation() -> None:
    """生产链路：每次 spawn 都必须透传隔离参数（树杀前置，缺则无法杀到后代）。"""
    tool = BashTool()
    mock_proc = _make_stream_proc()
    sentinel = {"__isolation_marker__": "present"}
    with (
        patch.object(BashTool, "_spawn_kwargs", return_value=sentinel) as spawn_kwargs,
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as spawn,
    ):
        await tool.execute({"command": "ls"})
    spawn_kwargs.assert_called_once()
    assert spawn.call_args.kwargs.get("__isolation_marker__") == "present", (
        "create_subprocess_exec 必须收到 spawn 隔离参数"
    )


@pytest.mark.asyncio
async def test_contract_timeout_tree_kills_and_reaps() -> None:
    """超时路径：树杀整棵子树（非首进程）、随后收割 root、抛 TimeoutExpired。"""
    tool = BashTool()

    class _HangStream:
        async def read(self, n: int = -1) -> bytes:
            await asyncio.Event().wait()
            return b""

    mock_proc = MagicMock()
    mock_proc.stdout = _HangStream()
    mock_proc.stderr = _HangStream()
    mock_proc.pid = 4321
    mock_proc.wait = AsyncMock()
    mock_proc.kill = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(BashTool, "_kill_process_tree", MagicMock()) as mock_tree,
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            await tool.execute({"command": "ls", "timeout": 1})
    mock_tree.assert_called_once_with(mock_proc), "超时后必须调用树杀（带整个 proc）"
    mock_proc.kill.assert_not_called(), "禁止回退到只杀首进程"
    mock_proc.wait.assert_awaited_once(), "树杀后必须收割 root，防止残留句柄"


@pytest.mark.asyncio
async def test_contract_cancel_tree_kills_and_reaps() -> None:
    """取消路径：与超时同契约——树杀、收割、re-raise，不得只杀首进程。"""
    tool = BashTool()

    class _CancelStream:
        async def read(self, n: int = -1) -> bytes:
            raise asyncio.CancelledError()

    mock_proc = MagicMock()
    mock_proc.stdout = _CancelStream()
    mock_proc.stderr = _CancelStream()
    mock_proc.pid = 4321
    mock_proc.wait = AsyncMock()
    mock_proc.kill = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(BashTool, "_kill_process_tree", MagicMock()) as mock_tree,
    ):
        with pytest.raises(asyncio.CancelledError):
            await tool.execute({"command": "ls", "timeout": 1})
    mock_tree.assert_called_once_with(mock_proc), "取消后必须调用树杀（带整个 proc）"
    mock_proc.kill.assert_not_called(), "禁止回退到只杀首进程"
    mock_proc.wait.assert_awaited_once(), "取消后必须收割 root"


def test_contract_tree_kill_primitive_posix_uses_killpg() -> None:
    """posix 原语：通过 getpgid 定位进程组并 SIGKILL 整组。"""
    proc = MagicMock(pid=777)
    with (
        patch("bandi.tools.subprocess_tree.sys.platform", "linux"),
        patch("bandi.tools.subprocess_tree.os.getpgid", return_value=999, create=True) as getpgid,
        patch("bandi.tools.subprocess_tree.os.killpg", create=True) as killpg,
    ):
        BashTool._kill_process_tree(proc)
    getpgid.assert_called_once_with(777)
    killpg.assert_called_once_with(999, _SIGKILL)


def test_contract_tree_kill_posix_best_effort_process_gone() -> None:
    """posix 原语：进程已退出（getpgid/killpg 抛 ProcessLookupError）不得再抛。"""
    proc = MagicMock(pid=777)
    with (
        patch("bandi.tools.subprocess_tree.sys.platform", "linux"),
        patch(
            "bandi.tools.subprocess_tree.os.getpgid",
            side_effect=ProcessLookupError,
            create=True,
        ),
        patch("bandi.tools.subprocess_tree.os.killpg", create=True),
    ):
        BashTool._kill_process_tree(proc)


def test_contract_tree_kill_primitive_win32_uses_taskkill_tree() -> None:
    """win32 原语：taskkill /T /F 杀整棵父-子树，非零返回码 best-effort 吞掉。"""
    with (
        patch("bandi.tools.subprocess_tree.sys.platform", "win32"),
        patch("bandi.tools.subprocess_tree.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        BashTool._kill_process_tree(MagicMock(pid=4242))
    kwargs = mock_run.call_args.kwargs
    assert kwargs["check"] is False, "死进程 taskkill 返回非零，必须 best-effort"
    assert kwargs["capture_output"] is True
    args = mock_run.call_args.args[0]
    assert args[0].lower() == "taskkill"
    assert "/PID" in args and args[args.index("/PID") + 1] == "4242"
    assert "/T" in args, "必须带 /T（整棵树）而非只杀首进程"
    assert "/F" in args


def test_contract_tree_kill_win32_best_effort_run_failure() -> None:
    """win32 原语：taskkill 启动失败（OSError）不得阻断超时/取消异常流。"""
    with (
        patch("bandi.tools.subprocess_tree.sys.platform", "win32"),
        patch("bandi.tools.subprocess_tree.subprocess.run", side_effect=OSError("boom")),
    ):
        BashTool._kill_process_tree(MagicMock(pid=1))


def _stopped_ticking(path: str, timeout: float, settle: float = 1.0) -> bool:
    """心跳文件内容在 settle 秒内不再变化 → 写进程已死（PID 复用无关，确定性）。"""
    last: str | None = None
    last_change = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
        except OSError:
            cur = None
        if cur != last:
            last = cur
            last_change = time.monotonic()
        elif cur is not None and time.monotonic() - last_change >= settle:
            return True
        time.sleep(0.1)
    return False


def _child_pid(heartbeat: str) -> int | None:
    try:
        with open(heartbeat, encoding="utf-8") as fh:
            text = fh.read().strip()
        if " " in text:
            return int(text.split(" ", 1)[0])
    except (OSError, ValueError):
        return None
    return None


def _best_effort_kill_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=False)
    except OSError:
        pass


def test_contract_tree_kill_real_process_reaps_descendants() -> None:
    """端到端真实进程：隔离 spawn 一棵 python 树 → 树杀 → root 回收且后代心跳停止。"""
    with tempfile.TemporaryDirectory() as td:
        heartbeat = os.path.join(td, "heartbeat")
        root_marker = os.path.join(td, "root_ready")
        root_script = os.path.join(td, "root.py")
        child_script = os.path.join(td, "child.py")

        Path(child_script).write_text(
            "import os, pathlib, time\n"
            f"hb = pathlib.Path(r'{heartbeat}')\n"
            "while True:\n"
            "    hb.write_text(str(os.getpid()) + ' ' + str(time.monotonic()))\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        Path(root_script).write_text(
            "import subprocess, sys, time, pathlib\n"
            f"subprocess.Popen([sys.executable, r'{child_script}'])\n"
            f"pathlib.Path(r'{root_marker}').write_text('1')\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )

        root: subprocess.Popen | None = None
        try:
            root = subprocess.Popen(
                [sys.executable, str(root_script)],
                **BashTool._spawn_kwargs(),
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not os.path.exists(root_marker):
                time.sleep(0.05)
            assert os.path.exists(root_marker), "root 未在限定时间内就绪"
            while time.monotonic() < deadline and not os.path.exists(heartbeat):
                time.sleep(0.05)
            assert os.path.exists(heartbeat), "后代未在限定时间内开始心跳"

            BashTool._kill_process_tree(root)
            assert root.wait(timeout=10) is not None, "root 未随树杀回收"
            assert _stopped_ticking(heartbeat, 5.0), "后代心跳未停止——树杀没杀到整棵子树"
        finally:
            child_pid = _child_pid(heartbeat) if os.path.exists(heartbeat) else None
            if child_pid is not None:
                _best_effort_kill_pid(child_pid)
            if root is not None and root.poll() is None:
                BashTool._kill_process_tree(root)
                try:
                    root.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    root.kill()


def test_contract_bash_finally_releases_job_on_communicate_exception() -> None:
    """TMC-接线契约（5.4 同步）：communicate 抛非超时/取消异常
    冒泡时，bash.execute 的 finally 必须释放 job handle——起 spawn 经统一
    托管原语（原语内 assign），bash 只负责 release 接线。"""
    import asyncio as _asyncio

    import bandi.tools.bash as bash_mod

    released: list[int] = []
    proc = MagicMock(pid=4242)

    class _BoomStream:
        async def read(self, n: int = -1) -> bytes:
            raise OSError("boom")

    proc.stdout = _BoomStream()
    proc.stderr = _BoomStream()

    async def _fake_spawn(*args: object, **kwargs: object) -> MagicMock:
        return proc

    with (
        patch.object(bash_mod, "spawn_managed_subprocess", _fake_spawn),
        patch.object(
            bash_mod,
            "release_process_job",
            side_effect=lambda pid: released.append(pid),
        ),
        patch.object(bash_mod.BashTool, "_spawn_kwargs", return_value={}),
    ):
        tool = BashTool()
        try:
            _asyncio.run(tool.execute({"command": "ls"}))
        except OSError:
            pass
    assert 4242 in released, "communicate 异常冒泡后 finally 必须释放 job handle"
