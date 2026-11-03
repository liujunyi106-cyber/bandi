from __future__ import annotations

import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.tools.bash import BashTool


class _FakeStream:
    # 功能：模拟子进程输出流（read 分块返回，EOF 返回空）
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if not self._data:
            return b""
        if n is None or n < 0:
            data, self._data = self._data, b""
            return data
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def _make_mock_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    # 功能：构造读循环形态的子进程 mock（stdout/stderr 流 + returncode + pid）
    proc = MagicMock()
    proc.stdout = _FakeStream(stdout)
    proc.stderr = _FakeStream(stderr)
    proc.returncode = returncode
    proc.pid = 4321
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


class TestBashTool:
    def test_name_description(self) -> None:
        tool = BashTool()
        assert tool.name == "bash"
        assert tool.description == "Execute a shell command (read/write — use with caution)"

    @pytest.mark.asyncio
    async def test_execute_basic(self) -> None:
        tool = BashTool()
        mock_proc = _make_mock_proc(b"file1\nfile2\n")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked:
            result = await tool.execute({"command": "ls"})

        assert "file1" in result
        assert "file2" in result
        mocked.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sed_write_blocked_by_shell_safety(self) -> None:
        """sed w/e 写文件形态经 bash 执行必须被 shell_safety 纵深防御拦截。"""
        tool = BashTool()
        for cmd in (
            "sed -n -e '/x/p' -e '1w /tmp/evil' /etc/passwd",
            "sed -n 's/foo/bar/w /tmp/evil; p' file.txt",
            "awk 'BEGIN{system (\"id\")}' file.txt",
        ):
            with pytest.raises(ValueError, match="command denied|command requires confirmation"):
                await tool.execute({"command": cmd})

    @pytest.mark.asyncio
    async def test_execute_defaults_cwd_to_workspace_root(self) -> None:
        """
        传给子进程导致继承父进程 cwd（可能在工作区外），越界写守卫失去基准。"""
        from bandi.tools.fs_tool import workspace_root

        tool = BashTool()
        mock_proc = _make_mock_proc(b"ok")
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked:
            await tool.execute({"command": "pwd"})
        kwargs = mocked.call_args.kwargs
        assert kwargs.get("cwd") == str(workspace_root()), "无 cwd 时必须默认 workspace root"

    @pytest.mark.asyncio
    async def test_execute_unknown_command_requires_confirm(self) -> None:
        from bandi.tools.base import ToolPermissionContext
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _UI:
            def __init__(self):
                self.calls = 0

            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                self.calls += 1
                return True

        tool = BashTool()
        mock_proc = _make_mock_proc()
        ui = _UI()
        pipeline = PermissionsPipeline(user_interaction=ui)
        context = ToolPermissionContext(permissions_pipeline=pipeline)
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked:
            await tool.execute({"command": "cp a.txt /tmp/evil"}, context=context)
        mocked.assert_called_once()
        assert ui.calls == 1, "unknown command 必须经一次确认"

    @pytest.mark.asyncio
    async def test_execute_unknown_command_fail_closed_without_pipeline(self) -> None:
        tool = BashTool()
        with patch("asyncio.create_subprocess_exec") as mocked:
            with pytest.raises(ValueError):
                await tool.execute({"command": "cp a.txt /tmp/evil"})
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_timeout(self) -> None:
        tool = BashTool()

        class _HangingStream:
            # 功能：模拟长运行命令——read 永不返回，直到被取消
            async def read(self, n: int = -1) -> bytes:
                await asyncio.Event().wait()
                return b""

        mock_proc = MagicMock()
        mock_proc.stdout = _HangingStream()
        mock_proc.stderr = _HangingStream()
        mock_proc.pid = 4321

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch.object(mock_proc, "kill", MagicMock()) as mock_kill,
            patch.object(mock_proc, "wait", AsyncMock()) as mock_wait,
            patch.object(BashTool, "_kill_process_tree", MagicMock()) as mock_tree,
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                await tool.execute({"command": "ls", "timeout": 1})

        mock_tree.assert_called_once_with(mock_proc)
        mock_kill.assert_not_called()
        mock_wait.assert_awaited_once()

    def test_dependency_check(self) -> None:
        tool = BashTool()
        assert len(tool.dependencies) == 1
        dep = tool.dependencies[0]
        assert dep.type == "binary"
        assert dep.name == "powershell"

    def test_parse_ast_allow(self) -> None:
        result = BashTool._parse_ast("ls -la")
        assert result.risk_level.name == "ALLOW"
        assert result.command_name == "ls"

    @pytest.mark.asyncio
    async def test_cwd_must_be_within_workspace(self) -> None:
        """cwd 沙箱必须读 BANDI_WORKSPACE_ROOT 环境变量（与 fs_tool 一致）。"""
        import tempfile
        from pathlib import Path

        tool = BashTool()
        outside = Path(tempfile.gettempdir()) / "bandi_outside_cwd"
        outside.mkdir(exist_ok=True)
        try:
            with patch.dict(
                "os.environ", {"BANDI_WORKSPACE_ROOT": str(outside / "ws")}, clear=False
            ):
                with pytest.raises(ValueError, match="must be within workspace"):
                    await tool.execute({"command": "pwd", "cwd": str(outside)})
        finally:
            # 子目录，rmdir（非空）抛 WinError 145。改 rmtree（该目录本就是测试自建）。
            shutil.rmtree(outside, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_cwd_allowed_inside_workspace(self) -> None:
        import tempfile
        from pathlib import Path

        tool = BashTool()
        ws = Path(tempfile.gettempdir()) / "bandi_ws_cwd"
        ws.mkdir(exist_ok=True)
        mock_proc = _make_mock_proc()
        try:
            with (
                patch.dict("os.environ", {"BANDI_WORKSPACE_ROOT": str(ws)}, clear=False),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            ):
                result = await tool.execute({"command": "pwd", "cwd": str(ws)})
            assert "file1" not in result
        finally:
            ws.rmdir()

    @pytest.mark.asyncio
    async def test_relative_cwd_resolved_against_workspace(self) -> None:
        """
        cwd 解析（可能在 workspace 外），显式 cwd="." 必被误拒。"""
        import tempfile
        from pathlib import Path

        tool = BashTool()
        ws = Path(tempfile.gettempdir()) / "bandi_ws_relcwd"
        ws.mkdir(exist_ok=True)
        mock_proc = _make_mock_proc()
        try:
            with (
                patch.dict("os.environ", {"BANDI_WORKSPACE_ROOT": str(ws)}, clear=False),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                patch("os.getcwd", return_value=str(Path(tempfile.gettempdir()))),
            ):
                result = await tool.execute({"command": "pwd", "cwd": "."})
            assert "error" not in result.lower()
        finally:
            ws.rmdir()

    @pytest.mark.asyncio
    async def test_default_cwd_anchors_project_root_when_in_context(self) -> None:
        """契约：会话绑定 project_root（context.extra）时 bash 缺省 cwd 必须锚定
        项目根——与 fs 系工具同基准（冒烟：模型 bash 探路落全局 workspace
        相对路径 read 失败/产物错位）。无 project_root 时回落 workspace root。"""
        import tempfile
        from pathlib import Path

        from bandi.tools.base import ToolPermissionContext

        tool = BashTool()
        proj = Path(tempfile.gettempdir()) / "bandi_proj_cwd"
        proj.mkdir(exist_ok=True)
        ws = Path(tempfile.gettempdir()) / "bandi_ws_cwd_default"
        ws.mkdir(exist_ok=True)
        mock_proc = _make_mock_proc()
        try:
            with (
                patch.dict("os.environ", {"BANDI_WORKSPACE_ROOT": str(ws)}, clear=False),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked,
            ):
                ctx = ToolPermissionContext(extra={"project_root": str(proj)})
                await tool.execute({"command": "pwd"}, context=ctx)
            assert Path(mocked.call_args.kwargs.get("cwd") or "").resolve() == proj.resolve(), (
                "project_root 会话缺省 cwd 必须锚定项目根（与 fs 工具一致）"
            )
        finally:
            ws.rmdir()
            proj.rmdir()

    @pytest.mark.asyncio
    async def test_relative_cwd_resolved_against_project_root(self) -> None:
        """契约：project_root 会话中相对 cwd 以项目根为基解析（与 fs 相对路径同基准）。"""
        import shutil
        import tempfile
        from pathlib import Path

        from bandi.tools.base import ToolPermissionContext

        tool = BashTool()
        proj = Path(tempfile.gettempdir()) / "bandi_proj_relcwd"
        proj.mkdir(exist_ok=True)
        (proj / "sub").mkdir(exist_ok=True)
        ws = Path(tempfile.gettempdir()) / "bandi_ws_projrel"
        ws.mkdir(exist_ok=True)
        mock_proc = _make_mock_proc()
        try:
            with (
                patch.dict("os.environ", {"BANDI_WORKSPACE_ROOT": str(ws)}, clear=False),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked,
            ):
                ctx = ToolPermissionContext(extra={"project_root": str(proj)})
                await tool.execute({"command": "pwd", "cwd": "sub"}, context=ctx)
            assert (
                Path(mocked.call_args.kwargs.get("cwd") or "").resolve() == (proj / "sub").resolve()
            )
        finally:
            ws.rmdir()
            shutil.rmtree(proj)

    @pytest.mark.asyncio
    async def test_cwd_outside_project_and_workspace_rejected(self) -> None:
        """契约（破坏者）：project_root 会话中 cwd 落在项目与 workspace 双根之外
        仍必须拒绝——project_root 是放行附加根，不是放宽沙箱。"""
        import tempfile
        from pathlib import Path

        from bandi.tools.base import ToolPermissionContext

        tool = BashTool()
        proj = Path(tempfile.gettempdir()) / "bandi_proj_reject"
        proj.mkdir(exist_ok=True)
        ws = Path(tempfile.gettempdir()) / "bandi_ws_reject"
        ws.mkdir(exist_ok=True)
        outside = Path(tempfile.gettempdir()) / "bandi_outside_both"
        outside.mkdir(exist_ok=True)
        try:
            with patch.dict("os.environ", {"BANDI_WORKSPACE_ROOT": str(ws)}, clear=False):
                ctx = ToolPermissionContext(extra={"project_root": str(proj)})
                with pytest.raises(ValueError, match="must be within workspace"):
                    await tool.execute({"command": "pwd", "cwd": str(outside)}, context=ctx)
        finally:
            outside.rmdir()
            ws.rmdir()
            proj.rmdir()

    def test_parse_ast_deny_recursive_delete(self) -> None:
        result = BashTool._parse_ast("rm -rf /")
        assert result.risk_level.name == "DENY"
        assert "recursive delete" in result.reason

    def test_parse_ast_deny_dd(self) -> None:
        result = BashTool._parse_ast("dd if=/dev/zero of=/dev/sda")
        assert result.risk_level.name == "DENY"
        assert "raw device write" in result.reason

    def test_parse_ast_deny_chmod_777(self) -> None:
        result = BashTool._parse_ast("chmod 777 /etc/shadow")
        assert result.risk_level.name == "DENY"
        assert "permission bypass" in result.reason

    def test_parse_ast_deny_pipeline_to_sh(self) -> None:
        result = BashTool._parse_ast("echo hello | sh")
        assert result.risk_level.name == "DENY"
        assert "dangerous" in result.reason

    def test_parse_ast_warn_git_mutation(self) -> None:
        result = BashTool._parse_ast("git push origin main")
        assert result.risk_level.name == "WARN"
        assert "git mutation" in result.reason

    def test_parse_ast_warn_rm_file(self) -> None:
        result = BashTool._parse_ast("rm old_file.txt")
        assert result.risk_level.name == "WARN"
        assert "file deletion" in result.reason

    @pytest.mark.asyncio
    async def test_parse_ast_execute_denied(self) -> None:
        tool = BashTool()
        with pytest.raises(ValueError, match="command denied"):
            await tool.execute({"command": "rm -rf /"})

    @pytest.mark.asyncio
    async def test_execute_warn_without_ui_denied(self) -> None:
        """
        不得静默执行，也不得绕过确认直接放行）。"""
        tool = BashTool()
        with pytest.raises(ValueError, match="requires confirmation"):
            await tool.execute({"command": "git push origin main"})

    @pytest.mark.asyncio
    async def test_execute_warn_with_ui_approval_runs(self) -> None:
        """
        用户批准后仍 100% 拒绝（功能断裂）。"""
        from bandi.tools.base import ToolPermissionContext
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _AllowUI:
            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                return True

        tool = BashTool()
        pipeline = PermissionsPipeline(user_interaction=_AllowUI())
        ctx = ToolPermissionContext(permissions_pipeline=pipeline)
        mock_proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mocked:
            result = await tool.execute({"command": "rm nonexistent_file.txt"}, context=ctx)
        assert "error" not in result.lower() or mocked.called
        mocked.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_warn_with_ui_denial_blocked(self) -> None:
        from bandi.tools.base import ToolPermissionContext
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _DenyUI:
            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                return False

        tool = BashTool()
        pipeline = PermissionsPipeline(user_interaction=_DenyUI())
        ctx = ToolPermissionContext(permissions_pipeline=pipeline)
        with pytest.raises(ValueError, match="requires confirmation"):
            await tool.execute({"command": "git push origin main"}, context=ctx)

    def test_parse_ast_ps_remove_item_recursive_denied(self) -> None:
        """
        bash 命令名集合不覆盖，Windows 默认 shell 下静默执行）。"""
        result = BashTool._parse_ast("Remove-Item -Recurse -Force C:\\Users\\x")
        assert result.risk_level.name == "DENY"
        assert "recursive delete" in result.reason

    def test_parse_ast_ps_remove_item_case_insensitive(self) -> None:
        result = BashTool._parse_ast("remove-item -r -f C:\\tmp\\x")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_ps_format_volume_denied(self) -> None:
        result = BashTool._parse_ast("Format-Volume -DriveLetter C")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_ps_clear_content_denied(self) -> None:
        result = BashTool._parse_ast("Clear-Content C:\\logs\\app.log")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_ps_icacls_denied(self) -> None:
        result = BashTool._parse_ast("icacls C:\\x /grant Everyone:F")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_ps_invoke_expression_denied(self) -> None:
        result = BashTool._parse_ast("Invoke-Expression 'rm -rf /'")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_ps_remove_item_plain_warn(self) -> None:
        result = BashTool._parse_ast("Remove-Item old_file.txt")
        assert result.risk_level.name == "WARN"

    def test_parse_ast_ps_stop_process_warn(self) -> None:
        result = BashTool._parse_ast("Stop-Process -Name python")
        assert result.risk_level.name == "WARN"

    def test_parse_ast_ps_pipeline_deny(self) -> None:
        """PowerShell 管道中的破坏性命令同样拦截（Get-Content | Remove-Item -Force）。"""
        result = BashTool._parse_ast("Get-Content C:\\x | Remove-Item -Force")
        assert result.risk_level.name == "DENY"

    def test_parse_ast_interpreter_prefix_denied(self) -> None:
        """深度复审修复：解释器前缀形态必须拦截——`pwsh -Command Remove-Item`
        / `powershell -Command` / `cmd /c rmdir /s /q` / `-EncodedCommand`
        """
        for cmd in (
            "pwsh -Command Remove-Item -Recurse C:\\foo",
            "powershell -Command Remove-Item -Recurse C:\\foo",
            'pwsh -Command "rm -rf C:\\foo"',
            "cmd /c rmdir /s /q C:\\foo",
            "cmd.exe /c del /f /s /q C:\\foo",
            "powershell -EncodedCommand SQBFAFgA",
        ):
            result = BashTool._parse_ast(cmd)
            assert result.risk_level.name == "DENY", (
                f"解释器前缀形态必须 DENY：{cmd!r} 实际 {result.risk_level.name}"
            )

    def test_parse_ast_shell_safety_interpreter_prefix_denied(self) -> None:
        """shell_safety 层（纵深防御）：pwsh/powershell/cmd 前缀必须 DENIED。"""
        from bandi.company.shell_safety import ShellSafetyLevel, classify_shell_command

        for cmd in (
            "pwsh -Command Remove-Item -Recurse C:\\foo",
            "powershell -Command Remove-Item -Recurse C:\\foo",
            "cmd /c rmdir /s /q C:\\foo",
            "powershell -EncodedCommand SQBFAFgA",
        ):
            result = classify_shell_command(cmd)
            assert result.level in (ShellSafetyLevel.DENIED, ShellSafetyLevel.CAUTION), (
                f"shell_safety 不得放行解释器前缀：{cmd!r} 实际 {result.level}"
            )

    def test_parse_ast_interpreter_prefix_unknown_cmd_warn(self) -> None:
        """解释器前缀 + 未知/无害命令保持 CAUTION（不误伤合法场景）。"""
        from bandi.company.shell_safety import ShellSafetyLevel, classify_shell_command

        result = classify_shell_command("pwsh -Command Get-Date")
        assert result.level in (ShellSafetyLevel.DENIED, ShellSafetyLevel.CAUTION), (
            "pwsh 前缀一律不放行（无法验证内部命令安全性）"
        )

    def test_legacy_check_interpreter_prefix_denied(self) -> None:
        """深度复审修复：legacy 路径（tree-sitter 不可用）同样拦截解释器前缀。"""
        for cmd in (
            "pwsh -Command Remove-Item -Recurse C:\\foo",
            "powershell -Command Remove-Item -Recurse C:\\foo",
            "cmd /c rmdir /s /q C:\\foo",
        ):
            result = BashTool._legacy_check(cmd)
            assert result.risk_level.name == "DENY", (
                f"legacy 路径必须拦截解释器前缀：{cmd!r} 实际 {result.risk_level.name}"
            )


def test_rm_separated_flags_denied() -> None:
    """修复：rm -r -f 分离 flag 必须 DENY（修复前交集空降 WARN）。"""
    from bandi.tools.bash import BashTool

    result = BashTool._match_patterns("rm", ["-r", "-f", "/tmp/x"])
    assert result is not None and result.risk_level.name == "DENY"
    # 单独 -f（非递归）仍 WARN 不升级
    warn_only = BashTool._match_patterns("rm", ["-f", "file.txt"])
    assert warn_only is not None and warn_only.risk_level.name == "WARN"
    # 单独 -r（递归）DENY
    assert BashTool._match_patterns("rm", ["-r", "dir"]).risk_level.name == "DENY"
