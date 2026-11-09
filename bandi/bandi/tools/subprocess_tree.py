# 功能：子进程树隔离 spawn 参数 + 整棵子树强杀（bash/git_ops/python_exec 复用）
from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
from ctypes import wintypes
from typing import Any

_SIGKILL: int = getattr(signal, "SIGKILL", 9)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_LIMIT_JOB_TIME = 0x0004
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_JOB_HANDLES: dict[int, int] = {}


class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 — 对齐 Win32 SDK 类型名
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — 对齐 Win32 SDK 类型名
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — 对齐 Win32 SDK 类型名
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win32_job_api() -> Any | None:
    # 功能：返回 kernel32 引用（非 win32 返回 None；可 patch 供契约测试）
    if not hasattr(ctypes, "windll"):
        return None
    return ctypes.windll.kernel32


def spawn_tree_kwargs() -> dict[str, Any]:
    """进程树隔离 spawn 参数：win32 独立进程组 / posix 新会话（树杀前置）。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def spawn_managed_subprocess(*cmd: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """功能：统一托管 spawn 原语（5.4）——合并进程树隔离参数（CREATE_NEW_PROCESS_GROUP
    / 新会话）并对真实子进程做 win32 Job Object 托管（KILL_ON_JOB_CLOSE）；
    python_exec/git_ops/bash/MCP stdio 全部经此，杜绝「只有 bash 有 Job」缺口。

    Windows SelectorEventLoop（uvicorn `--reload`/`workers>1` 触发）不支持子进程：
    asyncio 抛**无参 NotImplementedError（str 为空）**——此处转译为可行动错误
    否则工具失败对模型/用户不可见（实证：空 error → 模型编造 → 幻觉拦截）。"""
    memory_mb = int(kwargs.pop("memory_mb", 0) or 0)
    cpu_seconds = int(kwargs.pop("cpu_seconds", 0) or 0)
    merged = {**spawn_tree_kwargs(), **kwargs}
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **merged)
    except NotImplementedError as exc:
        raise RuntimeError(
            "subprocess unavailable: current event loop is SelectorEventLoop "
            "(Windows) which cannot spawn processes; restart the kernel without "
            "uvicorn --reload / workers>1 to enable bash/python_exec/git tools"
        ) from exc
    if isinstance(proc.pid, int):
        if memory_mb or cpu_seconds:
            assign_process_to_job(proc.pid, memory_mb=memory_mb, cpu_seconds=cpu_seconds)
        else:
            assign_process_to_job(proc.pid)
    return proc


def subprocess_capability_warning(loop: Any) -> str | None:
    # 功能：子进程能力自检（启动告警数据源）——Windows SelectorEventLoop
    # 不支持子进程（uvicorn `--reload`/`workers>1`），返回可行动告警文本；
    # 支持子进程或非 Windows 返回 None
    if sys.platform != "win32":
        return None
    if not isinstance(loop, asyncio.SelectorEventLoop):
        return None
    return (
        "toolchain degraded: kernel event loop is SelectorEventLoop which cannot "
        "spawn subprocesses (uvicorn --reload / workers>1 on Windows); "
        "bash/python_exec/git tools will fail. Restart without --reload "
        "for the full toolchain."
    )


def assign_process_to_job(pid: int, *, memory_mb: int = 0, cpu_seconds: int = 0) -> bool:
    """win32 Job Object 托管子进程：KILL_ON_JOB_CLOSE + 可选内存/CPU 上限"""
    if sys.platform != "win32" or not isinstance(pid, int):
        return False
    try:
        k32 = _win32_job_api()
        if k32 is None:
            return False
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_mb > 0:
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
        if cpu_seconds > 0:
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_JOB_TIME
            info.BasicLimitInformation.PerJobUserTimeLimit = int(cpu_seconds) * 10_000_000
        ok = k32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            k32.CloseHandle(job)
            return False
        k32.OpenProcess.restype = wintypes.HANDLE
        hproc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not hproc:
            k32.CloseHandle(job)
            return False
        assigned = k32.AssignProcessToJobObject(job, hproc)
        k32.CloseHandle(hproc)
        if assigned:
            _JOB_HANDLES[pid] = job
            return True
        k32.CloseHandle(job)
        return False
    except Exception:  # noqa: BLE001 — Job Object 失败不阻断主执行流（taskkill 兜底）
        return False


def release_process_job(pid: int) -> None:
    """释放 Job Object handle（TMC-泄漏修复）：进程正常退出/已回收后调用
    关闭 handle 触发 KILL_ON_JOB_CLOSE 清理残留后代；非 win32/无 job 安全 no-op。"""
    job = _JOB_HANDLES.pop(pid, None)
    if job is None or sys.platform != "win32":
        return
    try:
        k32 = _win32_job_api()
        if k32 is not None:
            k32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """强杀整棵进程树（含后代）；进程已死/原语失败时 best-effort 不抛。

    win32：Job Object TerminateJobObject 整树优先（含脱离父链的后代）
    再 taskkill /T /F 兜底双保险；posix：killpg 整组。
    """
    if sys.platform == "win32":
        job = _JOB_HANDLES.pop(proc.pid, None)
        if job:
            try:
                k32 = _win32_job_api()
                if k32 is not None:
                    k32.TerminateJobObject(job, 1)
                k32.CloseHandle(job) if k32 is not None else None
            except Exception:  # noqa: BLE001
                pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        if proc.pid is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), _SIGKILL)
        except OSError:
            pass


# 回收兜底超时——树杀 best-effort 失败（wait 永不返回）时不得阻塞取消/超时路径
_REAP_TIMEOUT_S: float = 2.0


async def reap_process(proc: asyncio.subprocess.Process) -> None:
    """功能：收割残留子进程——先温柔杀，wait 带 2s 超时；强杀失败且超时
    不抛出原异常（供 bash/git_ops/python_exec 取消与超时路径使用）。"""
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — 收割兜底：超时/任意错误均不覆盖原异常。
        pass


async def read_capped(stream: Any, cap: int) -> tuple[bytes, bool]:
    """功能：读时即限流——驻留内存不超过 cap 字节，超出部分继续排空（防大输出
    OOM 且不阻塞子进程管道写端），返回 (数据, 是否发生截断)。"""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while True:
        chunk: bytes = await stream.read(65536)
        if not chunk:
            break
        if len(buf) >= cap:
            truncated = True
            continue
        room = cap - len(buf)
        buf.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    return bytes(buf), truncated
