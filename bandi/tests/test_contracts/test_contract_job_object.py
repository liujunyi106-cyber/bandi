"""
契约族：Windows Job Object 进程树托管（残余 TMC-）。

背景：win32 进程树清理当前依赖 taskkill /T /F——对"已脱离父链/后台派生"
的进程可能漏杀；审计建议使用 Job Object（KILL_ON_JOB_CLOSE + TerminateJobObject
整树终止）。本契约落地 ctypes 无第三方依赖的 Job Object 托管：
- spawn 后 AssignProcessToJobObject（KILL_ON_JOB_CLOSE 保证 job 关闭即整树终止）
- 超时/取消树杀时 TerminateJobObject 优先，taskkill /T /F 兜底（双保险）

本契约钉的行为族（生产调用路径）：
- `tools/subprocess_tree.py::assign_process_to_job`（bash.py spawn 后调用）
- `tools/subprocess_tree.py::kill_process_tree`（win32 分支）

断言「最终产物」：win32 下 assign 必须完成 CreateJobObjectW → SetInformationJobObject
（KILL_ON_JOB_CLOSE）→ AssignProcessToJobObject 全链路；kill 必须优先
TerminateJobObject；非 win32 assign 必须安全返回 False（不抛）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bandi.tools.subprocess_tree import assign_process_to_job, kill_process_tree

_KILL_ON_JOB_CLOSE = 0x2000


def _win32_env(kernel32: MagicMock):
    return patch("bandi.tools.subprocess_tree.sys.platform", "win32"), patch(
        "bandi.tools.subprocess_tree._win32_job_api", return_value=kernel32
    )


def test_contract_assign_creates_job_with_kill_on_close() -> None:
    """win32 assign：CreateJobObjectW + SetInformationJobObject(KILL_ON_JOB_CLOSE) + AssignProcessToJobObject。"""
    k32 = MagicMock()
    k32.CreateJobObjectW.return_value = 12345
    k32.OpenProcess.return_value = 67890
    captured: dict = {}

    def _set_info(job, info_class, lpinfo, size):
        captured["lpinfo"] = lpinfo
        return True

    k32.SetInformationJobObject.side_effect = _set_info
    with _win32_env(k32)[0], _win32_env(k32)[1]:
        ok = assign_process_to_job(4242)
    assert ok is True, "win32 下 assign 必须成功"
    k32.CreateJobObjectW.assert_called_once()
    # SetInformationJobObject 必须设置 KILL_ON_JOB_CLOSE 标志
    set_info = k32.SetInformationJobObject
    assert set_info.called, "必须调用 SetInformationJobObject"
    args = set_info.call_args[0]
    assert args[0] == 12345, "必须操作刚创建的 job handle"
    assert args[1] == 9, "JobObjectExtendedLimitInformation=9"
    info_obj = captured["lpinfo"]._obj
    flags = info_obj.BasicLimitInformation.LimitFlags
    assert flags & _KILL_ON_JOB_CLOSE, (
        f"LimitFlags 必须含 KILL_ON_JOB_CLOSE(0x2000)，实际 {flags:#x}"
    )
    k32.OpenProcess.assert_called_once()
    k32.AssignProcessToJobObject.assert_called_once_with(12345, 67890)


def test_contract_assign_non_win32_safe_false() -> None:
    """非 win32：assign 必须安全返回 False（不调用任何 ctypes）。"""
    k32 = MagicMock()
    with patch("bandi.tools.subprocess_tree.sys.platform", "linux"):
        ok = assign_process_to_job(7)
    assert ok is False
    k32.CreateJobObjectW.assert_not_called()


def test_contract_kill_win32_prefers_terminate_job() -> None:
    """win32 kill：有 job 时优先 TerminateJobObject 整树，taskkill 兜底仍执行。"""
    k32 = MagicMock()
    proc = MagicMock(pid=99)
    with (
        _win32_env(k32)[0],
        _win32_env(k32)[1],
        patch("bandi.tools.subprocess_tree._JOB_HANDLES", {99: 555}),
        patch("bandi.tools.subprocess_tree.subprocess.run") as mrun,
    ):
        mrun.return_value = MagicMock(returncode=0)
        kill_process_tree(proc)
    k32.TerminateJobObject.assert_called_once_with(555, 1), "必须 TerminateJobObject 整树"
    assert mrun.called, "taskkill /T /F 兜底仍执行（双保险）"
    args = mrun.call_args.args[0]
    assert args[0].lower() == "taskkill" and "/T" in args


def test_contract_kill_win32_no_job_falls_back_taskkill() -> None:
    """win32 kill：无 job 句柄时直接 taskkill /T /F，不崩。"""
    k32 = MagicMock()
    proc = MagicMock(pid=4242)
    with (
        _win32_env(k32)[0],
        _win32_env(k32)[1],
        patch("bandi.tools.subprocess_tree._JOB_HANDLES", {}),
        patch("bandi.tools.subprocess_tree.subprocess.run") as mrun,
    ):
        mrun.return_value = MagicMock(returncode=1)
        kill_process_tree(proc)
    k32.TerminateJobObject.assert_not_called()
    args = mrun.call_args.args[0]
    assert args[0].lower() == "taskkill" and "/T" in args and "/F" in args


def test_contract_release_closes_job_handle() -> None:
    """release_process_job：正常退出路径必须 CloseHandle 并清账（防 handle 泄漏）。"""
    k32 = MagicMock()
    from bandi.tools.subprocess_tree import _JOB_HANDLES, release_process_job

    _JOB_HANDLES[4242] = 777
    with _win32_env(k32)[0], _win32_env(k32)[1]:
        release_process_job(4242)
    k32.CloseHandle.assert_called_once_with(777), "正常完成必须 CloseHandle"
    assert 4242 not in _JOB_HANDLES, "release 后记账必须清除"
    # 无 job / 非 win32：安全 no-op
    k32.reset_mock()
    with _win32_env(k32)[0], _win32_env(k32)[1]:
        release_process_job(999)
    k32.CloseHandle.assert_not_called()
