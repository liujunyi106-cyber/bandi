# 功能：Python 执行工具（PythonExecTool 沙箱执行代码）
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.subprocess_tree import (
    kill_process_tree,
    read_capped,
    reap_process,
    release_process_job,
    spawn_managed_subprocess,
)


class PythonExecTool(BaseTool):
    name: str = "python_exec"
    description: str = "Execute Python code in an isolated subprocess with timeout"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "high"
    permissions_override: str = "ask"
    # 拒绝松弛为审批后可执行任意代码
    permission_action: str = "execute"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["code"],
        "additionalProperties": False,
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 120)",
                "default": 30,
            },
        },
    }
    is_concurrency_safe: bool = False
    # 本地长时子进程工具——声明可取消：abort 竞速取消 + execute 内 CancelledError 分支杀进程树
    cancellable: bool = True

    def __init__(
        self,
        timeout_default: int = 30,
        timeout_max: int = 120,
        max_stdout_chars: int = 30000,
        max_stderr_chars: int = 10000,
    ):
        self._timeout_default = timeout_default
        self._timeout_max = timeout_max
        self._max_stdout_chars = max_stdout_chars
        self._max_stderr_chars = max_stderr_chars

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        code = params["code"]
        timeout = max(1, min(int(params.get("timeout", self._timeout_default)), self._timeout_max))

        from bandi.tools.isolation import (
            IsolationError,
            docker_available,
            load_settings,
            python_command,
            sanitize_env,
            workspace_for,
        )

        settings = load_settings()
        workspace = workspace_for(context)
        tmp_path: str | None = None
        try:
            if settings.mode == "docker":
                if not docker_available():
                    raise IsolationError(
                        "execution isolation mode=docker but Docker is unavailable; "
                        "refusing host execution"
                    )
                cmd, env, cwd = python_command(code, context)
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", encoding="utf-8", delete=False
                ) as f:
                    f.write(code)
                    tmp_path = f.name
                cmd = [sys.executable, tmp_path]
                env = sanitize_env(os.environ, workspace)
                cwd = str(workspace)

            proc = await spawn_managed_subprocess(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                memory_mb=settings.memory_mb,
                cpu_seconds=settings.cpu_seconds,
            )
            try:
                try:
                    _reads = asyncio.gather(
                        read_capped(proc.stdout, self._max_stdout_chars * 4),
                        read_capped(proc.stderr, self._max_stderr_chars * 4),
                    )
                    (stdout, out_truncated), (stderr, err_truncated) = await asyncio.wait_for(
                        _reads, timeout=timeout
                    )
                    await reap_process(proc)
                except asyncio.CancelledError:
                    # 外部取消（executor wait_for 超时/用户中断）必须杀整棵进程树
                    # 否则 python 子进程（含其 spawn 的后代）成为孤儿继续执行任意代码
                    kill_process_tree(proc)
                    await reap_process(proc)
                    raise
                except TimeoutError:
                    kill_process_tree(proc)
                    await reap_process(proc)
                    return {"ok": False, "error": f"timeout after {timeout}s", "exit_code": -1}

                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                truncated = out_truncated or err_truncated
                if len(stdout_str) > self._max_stdout_chars:
                    stdout_str = stdout_str[: self._max_stdout_chars] + "\n... [truncated]"
                    truncated = True
                if len(stderr_str) > self._max_stderr_chars:
                    stderr_str = stderr_str[: self._max_stderr_chars] + "\n... [truncated]"
                    truncated = True

                return {
                    "ok": proc.returncode == 0,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "exit_code": proc.returncode,
                    "truncated": truncated,
                }
            finally:
                release_process_job(proc.pid)
        finally:
            if tmp_path is not None:
                Path(tmp_path).unlink(missing_ok=True)
