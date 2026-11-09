# 功能：Git 操作工具（状态/diff/提交三工具）
from __future__ import annotations

import asyncio
from typing import Any

from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.subprocess_tree import (
    kill_process_tree,
    reap_process,
    release_process_job,
    spawn_managed_subprocess,
)

_GIT_TIMEOUT: int = 15


async def _run_git(
    args: list[str],
    cwd: str | None = None,
    timeout: int = 0,
    project_root: str | None = None,
) -> tuple[str, str, int]:
    from bandi.tools.fs_tool import resolve_within_workspace, workspace_root

    git_dir = cwd or project_root or str(workspace_root())
    # （project_root）为额外合法根——BUG-J 同族：漏注入致真实项目 git 全不可用
    git_dir = str(
        resolve_within_workspace(git_dir, extra_roots=[project_root] if project_root else None)
    )
    proc = await spawn_managed_subprocess(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=git_dir,
    )
    t = timeout if timeout > 0 else _GIT_TIMEOUT
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=t)
        except TimeoutError:
            kill_process_tree(proc)
            await reap_process(proc)
            raise
    finally:
        release_process_job(proc.pid)
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        proc.returncode if proc.returncode is not None else -1,
    )


class GitStatusTool(
    BaseTool
):  # 功能：显示 git 工作目录状态（git status --short + git log --oneline -5）
    name: str = "git_status"
    description: str = "Show git working tree status (modified/untracked/staged files)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cwd": {
                "type": "string",
                "description": "Git repository path (default: workspace root)",
            },
        },
    }

    def __init__(self, timeout: int = 0):
        self._timeout = timeout

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        cwd = params.get("cwd")
        out, err, code = await _run_git(["status", "--short"], cwd, self._timeout)
        if code != 0:
            raise RuntimeError(
                f"git status failed:\n{err}" if err else f"git status failed (exit {code})"
            )
        log_out, _, _ = await _run_git(["log", "--oneline", "-5"], cwd, self._timeout)
        parts = [
            f"=== git status ===\n{out.strip() or '(clean)'}",
            f"\n=== recent commits ===\n{log_out.strip() or '(no commits)'}",
        ]
        return "\n".join(parts)


class GitDiffTool(BaseTool):  # 功能：查看 git 工作目录/暂存区差异（git diff / git diff --staged）
    name: str = "git_diff"
    description: str = "Show git diff for modified files (working tree or staged)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged diff (--staged) instead of unstaged",
                "default": False,
            },
            "cwd": {
                "type": "string",
                "description": "Git repository path (default: workspace root)",
            },
        },
    }

    def __init__(self, timeout: int = 0):
        self._timeout = timeout

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        cwd = params.get("cwd")
        args = ["diff"]
        if params.get("staged"):
            args.append("--staged")
        out, err, code = await _run_git(args, cwd, self._timeout)
        if code != 0:
            raise RuntimeError(
                f"git diff failed:\n{err}" if err else f"git diff failed (exit {code})"
            )
        return out.strip() or "(no changes)"


class GitCommitTool(BaseTool):  # 功能：暂存所有更改并提交（git add -A && git commit -m msg）
    name: str = "git_commit"
    description: str = "Stage all changes and create a commit (requires user confirmation)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "medium"
    permissions_override: str = "ask"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["message"],
        "additionalProperties": False,
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message",
            },
            "cwd": {
                "type": "string",
                "description": "Git repository path (default: workspace root)",
            },
        },
    }
    is_concurrency_safe: bool = False

    def __init__(self, timeout: int = 0):
        self._timeout = timeout

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        cwd = params.get("cwd")
        msg = params["message"]
        _out1, err1, code1 = await _run_git(["add", "-A"], cwd, self._timeout)
        if code1 != 0:
            raise RuntimeError(
                f"git add failed:\n{err1}" if err1 else f"git add failed (exit {code1})"
            )
        out2, err2, code2 = await _run_git(["commit", "-m", msg], cwd, self._timeout)
        if code2 != 0:
            raise RuntimeError(
                f"git commit failed:\n{err2}" if err2 else f"git commit failed (exit {code2})"
            )
        return f"committed:\n{out2.strip()}"
