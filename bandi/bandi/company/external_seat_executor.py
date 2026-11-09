# 功能：外部席位执行器——外部 Agent CLI 一次性执行（路线 真外聘）
#
# 设计：
# - 一次性执行：把外部合约（audience=external）拼装为 prompt，喂给外部引擎 CLI
# - 只换实现不动调度器：实现 SeatExecutor 协议，CompanyDispatcher 按
# - 降级语义：外部引擎不回报 token——total_tokens 如实标 None +
# token_accounting 显式说明，不盲信估计。
# 违规阻断 success，不静默放行。

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.company_runtime_contract import build_external_company_work_item_contract
from bandi.company.output_contract import validate_work_item_output
from bandi.company.seat_executor import SeatResult
from bandi.company.work_item import WorkItem
from bandi.tools.subprocess_tree import (
    kill_process_tree,
    reap_process,
    release_process_job,
    spawn_managed_subprocess,
)

logger = logging.getLogger(__name__)

# 各外部引擎的一次性执行命令（首元素为二进制名，shutil.which 解析；prompt 追加为末参）
DEFAULT_AGENT_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec"),
    "claude_code": ("claude", "-p"),
    "cursor": ("cursor-agent", "-p"),
    "opencode": ("opencode", "run"),
}

_DEFAULT_TIMEOUT_SECONDS = 600.0
# 字符；posix 单参数 MAX_ARG_STRLEN 128KiB。超长 fail-closed 显式报错，不依赖
# 深层 OSError（错误可诊断），也不改 stdin/临时文件通道（四 CLI 语义不统一）
_WIN32_ARGV_LIMIT = 32_700
_POSIX_SINGLE_ARG_LIMIT = 126_000


class ExternalSeatExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        agent_commands: dict[str, tuple[str, ...]] | None = None,
        command_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # 共享工作区：外部引擎在 workspace 内一次性执行，产物落盘可读回；
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._agent_commands: dict[str, tuple[str, ...]] = dict(DEFAULT_AGENT_COMMANDS)
        if agent_commands:
            self._agent_commands.update(agent_commands)
        self._command_timeout = command_timeout
        # finally 据此清理，防取消路径孤儿进程与重调度双跑双写窗口
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}

    # -- SeatExecutor 协议（无长连接状态；进程级状态在 _active_procs） --

    async def prepare_seat(self, runtime: CompanyRuntime, _work_item: WorkItem) -> None:
        return None

    async def checkpoint(self, runtime: CompanyRuntime) -> dict[str, Any]:
        return {
            "session": runtime.member_session_id(),
            "executor": "external",
            "active_procs": len(self._active_procs),
        }

    async def interrupt(self, runtime: CompanyRuntime) -> None:
        """中断入口（run_turn 之外的双保险）：树杀该 session 活动进程并清登记。"""
        proc = self._active_procs.pop(runtime.member_session_id(), None)
        if proc is not None and proc.returncode is None:
            kill_process_tree(proc)
            await reap_process(proc)

    async def shutdown(self) -> None:
        procs = list(self._active_procs.values())
        self._active_procs.clear()
        for proc in procs:
            if proc.returncode is None:
                kill_process_tree(proc)
                await reap_process(proc)

    # -- 执行 --

    def _build_prompt(self, work_item: WorkItem) -> str:
        """外部合约（audience=external 措辞）+ 任务正文——与 native 合约同源拼装。"""
        contract_text = build_external_company_work_item_contract(
            {
                "turn_type": work_item.turn_type.value
                if hasattr(work_item.turn_type, "value")
                else str(work_item.turn_type),
                "title": work_item.title,
                "metadata": dict(work_item.metadata),
            }
        )
        task_text = work_item.title
        if work_item.description:
            task_text = f"{work_item.title}\n\n任务描述：{work_item.description}"
        return f"{contract_text}\n\n{task_text}" if contract_text else task_text

    def _resolve_workspace(self, runtime: CompanyRuntime) -> Path:
        if self._workspace_root is not None:
            return self._workspace_root
        from bandi.company.comms import _safe_path_segment

        project = _safe_path_segment(runtime.project_id or "default", "project_id")
        return Path.home() / ".bandi" / "projects" / project / "workspace"

    @staticmethod
    def _argv_limit_result(
        agent: str, command: tuple[str, ...], actual: int, limit: int
    ) -> SeatResult:
        """argv 超长 fail-closed（显式可诊断，不送平台撞深层 OSError）。"""
        return SeatResult(
            success=False,
            output=(
                f"external execution rejected: prompt exceeds platform argv limit "
                f"({actual} > {limit} chars) — shorten the task description or "
                "split the work item"
            ),
            metadata={
                "external_agent": agent,
                "terminal_reason": "FAILED",
                "launch_command": " ".join(command),
                "argv_limit_exceeded": True,
            },
        )

    async def run_turn(self, runtime: CompanyRuntime, work_item: WorkItem) -> SeatResult:
        agent = str((work_item.runtime_metadata or {}).get("assigned_external_agent") or "").strip()
        if not agent:
            return SeatResult(
                success=False,
                output="external execution requires assigned_external_agent in runtime metadata",
                metadata={"external_agent": "", "terminal_reason": "FAILED"},
            )
        command = self._agent_commands.get(agent)
        if command is None:
            known = ", ".join(sorted(self._agent_commands))
            return SeatResult(
                success=False,
                output=f"unknown external agent: {agent!r} (known: {known})",
                metadata={"external_agent": agent, "terminal_reason": "FAILED"},
            )
        binary = shutil.which(command[0])
        if binary is None:
            return SeatResult(
                success=False,
                output=f"external agent binary not found on PATH: {command[0]}",
                metadata={
                    "external_agent": agent,
                    "terminal_reason": "FAILED",
                    "launch_command": " ".join(command),
                },
            )

        try:
            workspace = self._resolve_workspace(runtime)
        except ValueError as exc:
            return SeatResult(
                success=False,
                output=f"external execution rejected: {exc}",
                metadata={"external_agent": agent, "terminal_reason": "FAILED"},
            )
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return SeatResult(
                success=False,
                output=f"external workspace not writable: {workspace} ({exc})",
                metadata={"external_agent": agent, "terminal_reason": "FAILED"},
            )

        prompt = self._build_prompt(work_item)
        argv = [binary, *command[1:], prompt]
        # 不送到 CreateProcess/exec 撞深层 OSError
        argv_text_len = sum(len(a) for a in argv) + len(argv)
        if sys.platform == "win32" and argv_text_len > _WIN32_ARGV_LIMIT:
            return self._argv_limit_result(agent, command, argv_text_len, _WIN32_ARGV_LIMIT)
        if sys.platform != "win32" and len(prompt) > _POSIX_SINGLE_ARG_LIMIT:
            return self._argv_limit_result(agent, command, len(prompt), _POSIX_SINGLE_ARG_LIMIT)
        logger.info(
            "external seat one-shot: agent=%s workspace=%s cmd=%s",
            agent,
            workspace,
            " ".join(command),
        )
        timed_out = False
        try:
            proc = await spawn_managed_subprocess(
                *argv,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return SeatResult(
                success=False,
                output=f"external agent launch failed: {exc}",
                metadata={
                    "external_agent": agent,
                    "terminal_reason": "FAILED",
                    "launch_command": " ".join(command),
                },
            )
        session_id = runtime.member_session_id()
        # 兜底防脱离父链的后代），best-effort 不阻断
        self._active_procs[session_id] = proc
        # BaseException 不走 except TimeoutError），进程未退出即树杀 + 回收
        # 消灭「协程取消→孤儿进程继续写 workspace→租约过期重调度双跑双写」窗口；
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self._command_timeout
                )
            except TimeoutError:
                timed_out = True
        finally:
            self._active_procs.pop(session_id, None)
            if proc.returncode is None:
                kill_process_tree(proc)
                await reap_process(proc)
            release_process_job(proc.pid)
        if timed_out:
            return SeatResult(
                success=False,
                output=(
                    f"external agent {agent!r} timed out after "
                    f"{self._command_timeout:g}s (process killed)"
                ),
                metadata={
                    "external_agent": agent,
                    "terminal_reason": "FAILED",
                    "launch_command": " ".join(command),
                    "workspace": str(workspace),
                    "timed_out": True,
                    "total_tokens": None,
                    "token_accounting": (
                        "external engine: token usage not reported by agent CLI (EX-6)"
                    ),
                },
            )

        output = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_tail = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()[-500:]
        returncode = proc.returncode

        work_item.runtime_metadata["_last_output"] = output
        violation: dict[str, Any] | None = None
        try:
            report = validate_work_item_output(work_item)
        except Exception as exc:  # noqa: BLE001 — 校验器异常按违规处理（fail-closed）
            report = None
            violation = {
                "missing_keys": [],
                "format_errors": [f"输出契约校验器异常: {exc}"],
                "warnings": [],
            }
            logger.warning(
                "external output contract validator crashed for work item %s: %s",
                work_item.id,
                exc,
            )
        if report is not None and not report.valid:
            violation = {
                "missing_keys": report.missing_keys,
                "format_errors": report.format_errors,
                "warnings": report.warnings,
            }
            logger.warning(
                "external output contract violation for work item %s: %s",
                work_item.id,
                violation,
            )

        exit_ok = returncode == 0
        if exit_ok and not output:
            return SeatResult(
                success=False,
                output=f"external agent {agent!r} exited 0 but produced empty output",
                metadata={
                    "external_agent": agent,
                    "terminal_reason": "FAILED",
                    "returncode": returncode,
                    "launch_command": " ".join(command),
                    "workspace": str(workspace),
                    "stderr_tail": stderr_tail,
                    "total_tokens": None,
                    "token_accounting": (
                        "external engine: token usage not reported by agent CLI (EX-6)"
                    ),
                },
            )

        if not exit_ok:
            output = output or f"external agent {agent!r} exited {returncode}: {stderr_tail}"

        success = exit_ok and violation is None
        metadata: dict[str, Any] = {
            "external_agent": agent,
            "terminal_reason": "COMPLETED" if success else "FAILED",
            "returncode": returncode,
            "launch_command": " ".join(command),
            "workspace": str(workspace),
            "total_tokens": None,
            "token_accounting": "external engine: token usage not reported by agent CLI (EX-6)",
        }
        if stderr_tail:
            metadata["stderr_tail"] = stderr_tail
        if violation is not None:
            metadata["output_contract_violation"] = violation
            metadata["_original_output"] = output
        return SeatResult(
            success=success,
            output=(f"输出契约校验失败: {violation}" if violation is not None else output),
            metadata=metadata,
        )
