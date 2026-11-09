# 功能：Bash 工具（命令执行 + 风险分级 + 输出截断）
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from enum import Enum, auto
from pathlib import Path
from typing import Any

from bandi.tools.base import (
    BaseTool,
    RetryPolicy,
    ToolCategory,
    ToolDependency,
    ToolPermissionContext,
)
from bandi.tools.subprocess_tree import (
    kill_process_tree,
    read_capped,
    reap_process,
    release_process_job,
    spawn_managed_subprocess,
    spawn_tree_kwargs,
)

# 功能：命令输出读时上限（字节）——超出部分丢弃并标注，防 GB 级输出全量驻留内存
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024

# 功能：网络出口不可用特征（命令退出码 0 但实际失败）——判定为工具失败
_NETWORK_FAILURE_MARKERS: tuple[str, ...] = (
    "Could not resolve host",
    "Could not resolve",
    "Name or service not known",
    "Temporary failure in name resolution",
    "No such host is known",
    "curl: (6)",
    "curl: (7)",
    "curl: (28)",
    "Ping 请求找不到主机",
    "无法解析",
    "请求超时",
    "连接超时",
)

_TREE_SITTER_AVAILABLE: bool = False
_BASH_LANG: Any = None
_TS_LOCK: threading.Lock = threading.Lock()


def _lazy_init_tree_sitter() -> bool:
    global _TREE_SITTER_AVAILABLE, _BASH_LANG
    if _TREE_SITTER_AVAILABLE:
        return True
    with _TS_LOCK:
        if _TREE_SITTER_AVAILABLE:
            return True
        try:
            import tree_sitter_bash as _tsb
            from tree_sitter import Language as _Lang

            _BASH_LANG = _Lang(_tsb.language())
            _TREE_SITTER_AVAILABLE = True
        except ImportError:
            _TREE_SITTER_AVAILABLE = False
        return _TREE_SITTER_AVAILABLE


class RiskLevel(Enum):
    DENY = auto()
    WARN = auto()
    ALLOW = auto()


_DENY_PATTERNS: list[tuple[set[str], set[str], str]] = [
    ({"rm"}, {"-rf", "--recursive", "-fr", "-r"}, "recursive delete"),
    ({"dd"}, set(), "raw device write"),
    ({"chmod"}, {"777", "a+rwx", "a+x", "a+w"}, "permission bypass"),
    ({"chown"}, set(), "ownership change"),
    ({"mkfs", "fdisk", "parted", "format"}, set(), "filesystem destruction"),
    ({"shutdown", "reboot", "halt", "init"}, set(), "system shutdown"),
]

_WARN_PATTERNS: list[tuple[set[str], set[str], str]] = [
    ({"rm", "rmdir"}, set(), "file deletion"),
    ({"chmod"}, set(), "permission change"),
    ({"sudo", "doas", "su"}, set(), "privilege escalation"),
    ({"wget", "curl"}, set(), "network download"),
    ({"kill", "pkill", "killall"}, set(), "process termination"),
    ({"git"}, {"push", "commit", "merge", "rebase", "reset"}, "git mutation"),
]

# PowerShell 等价破坏命令——Windows 默认 shell 为 powershell 时
# PowerShell 命令名与 flag 大小写不敏感，统一小写匹配。
_PS_DENY_PATTERNS: list[tuple[set[str], set[str], str]] = [
    (
        {"remove-item", "del", "erase", "rd", "rmdir"},
        # /s 是 cmd 风格递归开关（rd /s、del /s）——漏网将整目录递归删除
        {"-recurse", "-r", "-force", "-f", "/s"},
        "recursive delete",
    ),
    ({"format-volume", "format"}, set(), "filesystem destruction"),
    ({"clear-content"}, set(), "file content wipe"),
    ({"icacls", "takeown", "set-acl"}, set(), "permission change"),
    ({"invoke-expression", "iex"}, set(), "arbitrary expression execution"),
    ({"stop-computer", "restart-computer"}, set(), "system shutdown"),
]

_PS_WARN_PATTERNS: list[tuple[set[str], set[str], str]] = [
    ({"remove-item", "del", "erase", "rd", "rmdir"}, set(), "file deletion"),
    ({"clear-content", "set-content", "add-content"}, set(), "file content change"),
    ({"start-process"}, set(), "process launch"),
    ({"invoke-webrequest", "invoke-restmethod", "curl.exe", "wget"}, set(), "network download"),
    ({"stop-process", "kill"}, set(), "process termination"),
    ({"set-executionpolicy"}, set(), "execution policy change"),
]


class BashASTResult:
    __slots__ = ("risk_level", "reason", "command_name", "args")

    def __init__(
        self,
        risk_level: RiskLevel = RiskLevel.ALLOW,
        reason: str = "",
        command_name: str = "",
        args: list[str] | None = None,
    ) -> None:
        self.risk_level = risk_level
        self.reason = reason
        self.command_name = command_name
        self.args = args or []


def _extract_inline_command(args: list[str]) -> list[str]:
    """提取解释器前缀后的内联命令（-Command/-c/-EncodedCommand
    参数）用于递归模式匹配。无内联命令参数时返回空列表。"""
    for i, a in enumerate(args):
        if a.lower() in ("-command", "-c", "-encodedcommand", "/c"):
            rest = args[i + 1 :]
            if not rest:
                return []
            joined = " ".join(rest).strip()
            if joined.startswith('"') and joined.endswith('"'):
                joined = joined[1:-1]
            return joined.split()
    return []


# 解释器前缀（含 .exe 后缀变体）——pwsh/powershell/cmd/python/node/perl/ruby 等
# 内联代码安全性不可验证，AST/legacy 双路径一律 DENY
_INTERPRETER_PREFIXES = frozenset(
    {
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
        "cmd",
        "cmd.exe",
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "node",
        "node.exe",
        "deno",
        "deno.exe",
        "bun",
        "bun.exe",
        "ruby",
        "ruby.exe",
        "perl",
        "perl.exe",
        "php",
        "php.exe",
        "lua",
        "lua.exe",
    }
)


class BashTool(BaseTool):  # 功能：执行 shell 命令（白名单+管道检测+超时上限+工作目录限制）
    name: str = "bash"
    description: str = "Execute a shell command (read/write — use with caution)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "high"
    permissions_override: str = "ask"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    permission_action: str = "execute"
    # 需要身份上下文（employee_id/project_id/employee_workspace）以隔离工作区
    needs_context: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["command"],
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 60)",
                "default": 30,
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (must be within workspace)",
            },
        },
    }
    is_concurrency_safe: bool = False
    # 本地长时子进程工具——声明可取消：abort 竞速取消 + execute 内 CancelledError 分支杀进程树
    cancellable: bool = True
    dependencies: list[ToolDependency] = [
        ToolDependency(
            type="binary", name="powershell", help_hint="Windows PowerShell is required"
        ),
    ]

    def __init__(self, timeout_default: int = 30, timeout_max: int = 60):
        self._timeout_default = timeout_default
        self._timeout_max = timeout_max

    def _resolve_shell(self) -> str:
        for candidate in ("bash", "pwsh", "powershell"):
            path = shutil.which(candidate)
            if path:
                return path
        comspec = os.environ.get("ComSpec")
        if comspec:
            return comspec
        return "powershell"

    def _build_command(self, command: str) -> list[str]:
        shell = self._resolve_shell()
        shell_name = os.path.splitext(os.path.basename(shell))[0].lower()
        if shell_name == "bash":
            return [shell, "-c", command]
        return [shell, "-Command", command]

    @staticmethod
    def _spawn_kwargs() -> dict[str, Any]:
        """进程树隔离 spawn 参数：win32 独立进程组 / posix 新会话（树杀前置）。"""
        return spawn_tree_kwargs()

    @staticmethod
    def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
        """强杀整棵进程树（含后代）；进程已死/原语失败时 best-effort 不抛。"""
        kill_process_tree(proc)

    @staticmethod
    async def _wait_reap(proc: asyncio.subprocess.Process) -> None:
        """兜底回收子进程（进程树已杀）——委托公共原语 reap_process（2s 超时
        树杀失败残留不阻塞取消/超时传播）。"""
        await reap_process(proc)

    @staticmethod
    def _parse_ast(command: str) -> BashASTResult:
        stripped = command.strip()
        if not stripped:
            return BashASTResult(RiskLevel.DENY, "empty command")

        _lazy_init_tree_sitter()
        if not _TREE_SITTER_AVAILABLE:
            return BashTool._legacy_check(stripped)

        from tree_sitter import Parser as _Parser

        parser = _Parser(_BASH_LANG)
        tree = parser.parse(bytes(stripped, "utf-8"))
        root = tree.root_node

        if root.child_count == 0:
            return BashASTResult(RiskLevel.DENY, "empty AST")

        first = root.child(0)
        if first is None:
            return BashASTResult(RiskLevel.DENY, "empty AST")

        # program 下的兄弟节点，只查 child(0) 会漏掉后续命令（换行注入）
        first_command_name = ""
        for i in range(root.child_count):
            node = root.child(i)
            if node is None or node.type == "comment":
                continue
            if node.type == "list":
                for j in range(node.child_count):
                    child = node.child(j)
                    if child is None:
                        continue
                    if child.type in ("command", "pipeline"):
                        result = BashTool._check_node(child)
                        if result.risk_level != RiskLevel.ALLOW:
                            return result
                        if not first_command_name:
                            first_command_name = (
                                child.child(0).text.decode() if child.child(0) else ""
                            )
                continue
            if node.type == "pipeline":
                result = BashTool._check_pipeline_node(node)
            elif node.type == "command":
                result = BashTool._check_node(node)
            else:
                # 未知节点类型 fail-closed 方向：交由确认通道而非静默放行
                result = BashASTResult(RiskLevel.WARN, f"unsupported AST node: {node.type}")
            if result.risk_level != RiskLevel.ALLOW:
                return result
            if not first_command_name and node.child(0):
                first_command_name = node.child(0).text.decode()

        return BashASTResult(RiskLevel.ALLOW, "", first_command_name)

    @staticmethod
    def evaluate_command(command: str) -> tuple[str, str]:
        """功能：命令执行前裁决——deny/confirm/allow + 原因（AST 与只读分类器单一实现）。"""
        parsed = BashTool._parse_ast(command)
        if parsed.risk_level == RiskLevel.DENY:
            return "deny", parsed.reason
        if parsed.risk_level == RiskLevel.WARN:
            return "confirm", parsed.reason
        from bandi.company.shell_safety import ShellSafetyLevel, classify_shell_command

        verdict = classify_shell_command(command)
        if verdict.level == ShellSafetyLevel.DENIED:
            return "deny", verdict.reason
        if verdict.level in (ShellSafetyLevel.DANGEROUS, ShellSafetyLevel.CAUTION):
            return "confirm", verdict.reason
        return "allow", ""

    @staticmethod
    def _match_patterns(cmd_name: str, args: list[str]) -> BashASTResult | None:
        # 命令名与 flag 统一小写比较——PowerShell 大小写不敏感；
        lower_name = cmd_name.lower()
        arg_set = {a.lower() for a in args}
        for cmds, flags, reason in (*_DENY_PATTERNS, *_PS_DENY_PATTERNS):
            if lower_name in cmds:
                if not flags or arg_set & flags:
                    return BashASTResult(RiskLevel.DENY, reason, cmd_name, args)
        for cmds, flags, reason in (*_WARN_PATTERNS, *_PS_WARN_PATTERNS):
            if lower_name in cmds:
                if not flags or arg_set & flags:
                    return BashASTResult(RiskLevel.WARN, reason, cmd_name, args)
        return None

    @staticmethod
    def _check_node(node: Any) -> BashASTResult:
        cmd_name_node = None
        args: list[str] = []
        for i in range(node.child_count):
            c = node.child(i)
            if c is None:
                continue
            if c.type == "command_name":
                cmd_name_node = c
            elif c.type in ("word", "number"):
                args.append(c.text.decode())

        if cmd_name_node is None:
            return BashASTResult(RiskLevel.DENY, "no command name")
        cmd_name = cmd_name_node.text.decode()
        # 解释器前缀形态必须拦截——`pwsh -Command Remove-Item` / `powershell
        # -Command` / `cmd /c rmdir /s /q` / `-EncodedCommand`，内联代码
        # 安全性不可验证，无法证明只读时一律 DENY
        lower = cmd_name.lower()
        if lower in _INTERPRETER_PREFIXES:
            # 内联命令参数（-Command/-c/-EncodedCommand）递归做模式匹配；
            # 无法验证内部命令安全性时一律 DENY（无法证明只读）
            inline = _extract_inline_command(args)
            if inline:
                result = BashTool._match_patterns(inline[0], inline[1:])
                if result is not None and result.risk_level == RiskLevel.DENY:
                    return result
            return BashASTResult(
                RiskLevel.DENY,
                f"interpreter prefix: {lower} (cannot verify safety)",
                cmd_name,
                args,
            )
        result = BashTool._match_patterns(cmd_name, args)
        if result is not None:
            return result
        return BashASTResult(RiskLevel.ALLOW, "", cmd_name, args)

    @staticmethod
    def _is_dangerous_command(node: Any) -> bool:
        for j in range(node.child_count):
            cc = node.child(j)
            if cc is not None and cc.type == "command_name":
                name = cc.text.decode()
                if name in ("sh", "bash", "python", "python3", "pwsh", "powershell"):
                    return True
                break
        return False

    @staticmethod
    def _check_pipeline_node(node: Any) -> BashASTResult:
        for i in range(node.child_count):
            c = node.child(i)
            if c is None:
                continue
            if c.type == "command":
                if BashTool._is_dangerous_command(c):
                    return BashASTResult(RiskLevel.DENY, "pipeline to dangerous interpreter")
                # 管道内每个命令走完整模式匹配（`Get-Content x | Remove-Item -Force` 不漏拦）
                result = BashTool._check_node(c)
                if result.risk_level != RiskLevel.ALLOW:
                    return result
        return BashASTResult(RiskLevel.ALLOW)

    @staticmethod
    def _legacy_check(command: str) -> BashASTResult:
        import re as _re

        stripped = command.strip()
        tokens = stripped.split()
        if not tokens:
            return BashASTResult(RiskLevel.DENY, "empty command")

        # legacy 路径对管道/顺序/换行分隔的每条命令同样模式匹配（tree-sitter
        # 不可用时复合命令不漏检——与 AST 全树遍历行为一致：
        # `Get-Content x | Remove-Item -Force` / `echo hi; rm -rf /x` /
        # `echo hi<newline>rm -rf /x` 均逐段拦截）
        normalized = stripped.replace("&&", ";").replace("||", ";")
        for seg in _re.split(r"[|;\n\r]+", normalized):
            seg_tokens = seg.strip().split()
            if not seg_tokens:
                continue
            seg_head = seg_tokens[0].lower()
            # legacy 管道段同样拦截解释器前缀（与 AST _check_pipeline_node
            # 逐段 _check_node 行为一致）——`echo x | python3 -c 'rm -rf /'` 拦截
            if seg_head in _INTERPRETER_PREFIXES:
                inline = _extract_inline_command(seg_tokens[1:])
                if inline:
                    result = BashTool._match_patterns(inline[0], inline[1:])
                    if result is not None and result.risk_level == RiskLevel.DENY:
                        return result
                return BashASTResult(
                    RiskLevel.DENY,
                    f"interpreter prefix: {seg_head} (cannot verify safety)",
                    seg_tokens[0],
                    seg_tokens[1:],
                )
            result = BashTool._match_patterns(seg_tokens[0], seg_tokens[1:])
            if result is not None:
                return result

        cmd_name = tokens[0]
        args = tokens[1:]
        # legacy 路径同样拦截解释器前缀（与 AST 路径一致）
        lower_head = cmd_name.lower()
        if lower_head in _INTERPRETER_PREFIXES:
            inline = _extract_inline_command(args)
            if inline:
                result = BashTool._match_patterns(inline[0], inline[1:])
                if result is not None and result.risk_level == RiskLevel.DENY:
                    return result
            return BashASTResult(
                RiskLevel.DENY,
                f"interpreter prefix: {lower_head} (cannot verify safety)",
                cmd_name,
                args,
            )
        dangerous = [r"\|\s*(sh|bash|python)\b", r"`[^`]+`", r"\$\([^)]+\)"]
        for p in dangerous:
            if _re.search(p, stripped):
                return BashASTResult(RiskLevel.DENY, "dangerous pattern", cmd_name, args)

        return BashASTResult(RiskLevel.ALLOW, "", cmd_name, args)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:  # 功能：执行 shell 命令，返回 stdout+stderr 文本
        command = params["command"]
        timeout = max(1, min(int(params.get("timeout", self._timeout_default)), self._timeout_max))
        cwd = params.get("cwd")

        verdict, reason = self.evaluate_command(command)
        if verdict == "deny":
            raise ValueError(f"command denied: {reason}")
        if verdict == "confirm":
            # 无确认通道（headless）时 fail-closed 拒绝。
            pipeline = (
                context.permissions_pipeline
                if context is not None and context.permissions_pipeline is not None
                else None
            )
            if pipeline is None or not await pipeline.confirm_tool(
                tool_name="bash",
                arguments=params,
                risk="medium",
                reason=reason,
            ):
                raise ValueError(f"command requires confirmation: {reason}")

        extra = getattr(context, "extra", None) or {}
        employee_ws = str(extra.get("employee_workspace") or "").strip()
        if cwd:
            from bandi.tools.fs_tool import project_extra_roots, workspace_root

            ws_root = workspace_root().resolve()
            proj = project_extra_roots(context)
            # 员工会话相对 cwd 以员工工作区为基准；非员工回退会话项目根
            anchor = (
                Path(employee_ws).resolve()
                if employee_ws
                else (Path(proj[0]).resolve() if proj else ws_root)
            )
            # 相对 cwd 以会话基准（project_root/employee_workspace 优先）解析
            resolved = Path(cwd).resolve() if Path(cwd).is_absolute() else (anchor / cwd).resolve()
            allowed = [ws_root] + ([Path(p).resolve() for p in proj] if proj else [])
            if employee_ws:
                allowed.append(Path(employee_ws).resolve())
            if not any(resolved.is_relative_to(a) for a in allowed):
                raise ValueError(f"cwd must be within workspace: {cwd}")
            cwd = str(resolved)  # 相对 cwd 须解析为绝对路径传给子进程（基准=会话根）
        else:
            # 避免继承父进程 cwd（可能在工作区外），越界写守卫的相对路径解析也
            # 探路落全局 workspace 致相对路径 read 失败/产物错位）
            from bandi.tools.fs_tool import project_extra_roots, workspace_root

            if employee_ws:
                # 员工默认工作目录 = 其独立工作区（产物不落共享项目根）
                cwd = employee_ws
            else:
                proj = project_extra_roots(context)
                cwd = str(Path(proj[0]).resolve()) if proj else str(workspace_root())

        from bandi.tools.isolation import (
            IsolationError,
            bash_command,
            docker_available,
            load_settings,
            sanitize_env,
            workspace_for,
        )

        settings = load_settings()
        workspace = workspace_for(context)
        if settings.mode == "docker":
            if not docker_available():
                raise IsolationError(
                    "execution isolation mode=docker but Docker is unavailable; "
                    "refusing host execution"
                )
            cmd, env, cwd = bash_command(command, context)
        else:
            cmd = self._build_command(command)
            env = sanitize_env(os.environ, workspace)
        proc = await spawn_managed_subprocess(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            memory_mb=settings.memory_mb,
            cpu_seconds=settings.cpu_seconds,
            **self._spawn_kwargs(),
        )

        try:
            try:
                _reads = asyncio.gather(
                    read_capped(proc.stdout, _MAX_CAPTURE_BYTES),
                    read_capped(proc.stderr, _MAX_CAPTURE_BYTES),
                )
                (stdout, out_truncated), (stderr, err_truncated) = await asyncio.wait_for(
                    _reads, timeout=timeout
                )
                await self._wait_reap(proc)
            except asyncio.CancelledError:
                self._kill_process_tree(proc)
                await self._wait_reap(proc)
                raise
            except TimeoutError:
                self._kill_process_tree(proc)
                await self._wait_reap(proc)
                raise subprocess.TimeoutExpired(
                    cmd,
                    timeout=timeout,
                    output=None,
                ) from None

            output = stdout.decode("utf-8", errors="replace")
            if out_truncated:
                output += "\n... [truncated]"
            if proc.returncode != 0:
                # 拼接后非空）即返回成功，失败命令被当成功吞掉返回码
                err = stderr.decode("utf-8", errors="replace")
                if err:
                    output += f"\n[stderr]\n{err}"
                raise RuntimeError(
                    f"command failed (exit {proc.returncode}){': ' + err.strip() if err.strip() else ''}"
                )
            # 网络出口不可用识别：PowerShell 探测命令退出码为 0 但输出失败信息，
            # 不标记失败会让模型陷入「网络诊断循环」耗尽轮次。
            err_text = stderr.decode("utf-8", errors="replace")
            combined = f"{output}\n{err_text}"
            for marker in _NETWORK_FAILURE_MARKERS:
                if marker in combined:
                    raise RuntimeError(
                        "network egress unavailable (repeated diagnostics will not help); "
                        "use knowledge/memory/existing artifacts and mark missing evidence as pending"
                    )

            return output
        finally:
            release_process_job(proc.pid)
