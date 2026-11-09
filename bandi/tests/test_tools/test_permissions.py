# 功能：PermissionsPipeline 单元测试 -- 7 层管道全覆盖（正常/边界/异常）
from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.permissions_pipeline import PermissionsPipeline


class SafeTool(BaseTool):
    name: str = "safe_tool"
    description: str = ""
    risk_level: str = "low"

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        return "ok"


class HighRiskTool(BaseTool):
    name: str = "high_risk"
    description: str = ""
    risk_level: str = "critical"

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        return "ok"


class TestPermissionsPipeline:
    @pytest.mark.asyncio
    async def test_bypass_tool_allowed(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_ask_tool_triggers_ask(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=False))
        tc = ToolCall(id="1", name="bash", arguments={"command": "ls"})
        verdict = await pipeline.check(tc, tool=SafeTool(), mode="execute")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_ask_tool_user_allows(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tc = ToolCall(id="1", name="bash", arguments={"command": "ls"})
        verdict = await pipeline.check(tc, tool=SafeTool(), mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_deny_list_blocks_tool(self) -> None:
        pipeline = PermissionsPipeline(deny_tools={"evil_tool"})
        tc = ToolCall(id="1", name="evil_tool", arguments={})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_no_user_interaction_denies_ask_tools(self) -> None:
        """无确认通道（headless/后台）时 ask 工具必须 fail-closed，不得静默放行。"""
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="bash", arguments={"command": "ls"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "approval" in verdict.reason or "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_no_user_interaction_bypass_tools_allowed(self) -> None:
        """回归：无 UI 只影响 ask 工具，bypass 只读工具仍放行。"""
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={"file_path": "x.py"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_ask_mode_treated_as_execute(self) -> None:
        """双模式化：ask 不再触发权限层拒绝，行为与 execute 一致。"""
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={})
        v_ask = await pipeline.check(tc, tool=None, mode="ask")
        v_execute = await pipeline.check(tc, tool=None, mode="execute")
        assert v_ask.allow == v_execute.allow

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_bash(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="bash", arguments={"command": "echo hi"})
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert not verdict.allow
        assert "plan mode" in verdict.reason

    @pytest.mark.asyncio
    async def test_plan_mode_allows_read(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={"path": "test.txt"})
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_plan_mode_allows_ontology_query(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="ontology_query", arguments={})
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_dangerous_shell_command_blocked(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tc = ToolCall(id="1", name="bash", arguments={"command": "echo hi | sh"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "dangerous shell" in verdict.reason

    @pytest.mark.asyncio
    async def test_sensitive_path_blocked(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={"path": "/home/user/.ssh/id_rsa"})
        tool = SafeTool()
        tool.name = "read"
        verdict = await pipeline.check(tc, tool=tool, mode="execute")
        assert not verdict.allow
        assert "sensitive" in verdict.reason

    @pytest.mark.asyncio
    async def test_deny_command_blocks_rm_rf(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="bash", arguments={"command": "rm -rf /"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_deny_command_blocks_powershell_destructive(self) -> None:
        """
        （大小写不敏感）——bash AST 层已拦，此处为独立防线回归。"""
        pipeline = PermissionsPipeline()
        for cmd in (
            "Remove-Item -Recurse -Force C:\\Users\\x",
            "remove-item -r C:\\tmp\\x",
            "Format-Volume -DriveLetter C",
            "Clear-Content C:\\logs\\app.log",
            "icacls C:\\x /grant Everyone:F",
            "Invoke-Expression 'rm -rf /'",
        ):
            tc = ToolCall(id="1", name="bash", arguments={"command": cmd})
            verdict = await pipeline.check(tc, tool=None, mode="execute")
            assert not verdict.allow, f"Layer 1 deny 必须拦截: {cmd}"

    @pytest.mark.asyncio
    async def test_deny_command_does_not_block_normal_command(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="bash", arguments={"command": "Remove-Item old.txt"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert "command denied" not in verdict.reason, (
            "普通 Remove-Item（无递归/强制）不属于 Layer 1 deny 列表（WARN 层处理）"
        )

    @pytest.mark.asyncio
    async def test_permissions_override_allow_cannot_bypass_safety(self) -> None:
        pipeline = PermissionsPipeline()
        tool = SafeTool()
        tool.permissions_override = "allow"
        tc = ToolCall(id="1", name="bash", arguments={"command": "rm -rf /"})
        verdict = await pipeline.check(tc, tool=tool, mode="ask")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_permissions_override_deny(self) -> None:
        pipeline = PermissionsPipeline()
        tool = SafeTool()
        tool.permissions_override = "deny"
        tc = ToolCall(id="1", name="read", arguments={"path": "test.txt"})
        verdict = await pipeline.check(tc, tool=tool, mode="execute")
        assert not verdict.allow
        assert "override" in verdict.reason

    @pytest.mark.asyncio
    async def test_empty_command_blocked(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tc = ToolCall(id="1", name="bash", arguments={"command": ""})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "empty" in verdict.reason

    @pytest.mark.asyncio
    async def test_sensitive_extension_blocked(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={"path": "/home/user/cert.pem"})
        tool = SafeTool()
        tool.name = "read"
        verdict = await pipeline.check(tc, tool=tool, mode="execute")
        assert not verdict.allow
        assert "sensitive" in verdict.reason

    @pytest.mark.asyncio
    async def test_user_denied_high_risk_tool(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=False))
        tc = ToolCall(id="1", name="high_risk", arguments={})
        verdict = await pipeline.check(tc, tool=HighRiskTool(), mode="execute")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_user_allows_high_risk_tool(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tc = ToolCall(id="1", name="high_risk", arguments={})
        verdict = await pipeline.check(tc, tool=HighRiskTool(), mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_tool_check_permissions_delegated(self) -> None:

        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))

        class GuardedTool(SafeTool):
            async def check_permissions(self, params, ctx):
                if params.get("dest", "").startswith("/etc"):
                    return False, "tool-level deny: /etc"
                return True, ""

        tool = GuardedTool()
        tool.name = "copy"
        # 工具不在 ask 列表 → 走委派层
        deny_call = ToolCall(id="1", name="copy", arguments={"src": "a.txt", "dest": "/etc/passwd"})
        verdict = await pipeline.check(deny_call, tool=tool, mode="execute")
        assert not verdict.allow, "工具级 check_permissions 拒绝必须生效"
        assert "tool-level deny" in verdict.reason

        allow_call = ToolCall(id="2", name="copy", arguments={"src": "a.txt", "dest": "b.txt"})
        verdict2 = await pipeline.check(allow_call, tool=tool, mode="execute")
        assert verdict2.allow, "工具级 check_permissions 放行必须生效"

    @pytest.mark.asyncio
    async def test_execute_mode_allows_safe_tool(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="read", arguments={})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_permissions_override_ask_user_denies(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=False))
        tool = SafeTool()
        tool.permissions_override = "ask"
        tc = ToolCall(id="1", name="safe_tool", arguments={})
        verdict = await pipeline.check(tc, tool=tool, mode="execute")
        assert not verdict.allow
        assert "denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_permissions_override_ask_user_allows(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tool = SafeTool()
        tool.permissions_override = "ask"
        tc = ToolCall(id="1", name="safe_tool", arguments={})
        verdict = await pipeline.check(tc, tool=tool, mode="execute")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_high_risk_shell_bash_blocked(self) -> None:
        pipeline = PermissionsPipeline(user_interaction=MockUserInteraction(allow=True))
        tc = ToolCall(id="1", name="bash", arguments={"command": "ls -la $(pwd)"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "dangerous" in verdict.reason

    # ── New tool permissions tests ──

    @pytest.mark.asyncio
    async def test_python_exec_triggers_ask(self) -> None:
        pipeline = PermissionsPipeline(
            ask_tools={"python_exec"}, user_interaction=MockUserInteraction(allow=False)
        )
        tc = ToolCall(id="1", name="python_exec", arguments={"code": "print(1)"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "user denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_python_exec_denied_in_plan(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(id="1", name="python_exec", arguments={"code": "print(1)"})
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert not verdict.allow

    @pytest.mark.asyncio
    async def test_web_fetch_is_bypass(self) -> None:
        pipeline = PermissionsPipeline(bypass_tools={"web_fetch"})
        tc = ToolCall(id="1", name="web_fetch", arguments={"url": "https://example.com"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow
        assert not verdict.ask_user

    @pytest.mark.asyncio
    async def test_web_fetch_allowed_in_plan(self) -> None:
        pipeline = PermissionsPipeline(bypass_tools={"web_fetch"})
        tc = ToolCall(id="1", name="web_fetch", arguments={"url": "https://example.com"})
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_browser_triggers_ask(self) -> None:
        pipeline = PermissionsPipeline(
            ask_tools={"browser"}, user_interaction=MockUserInteraction(allow=False)
        )
        tc = ToolCall(
            id="1", name="browser", arguments={"action": "navigate", "url": "https://example.com"}
        )
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert not verdict.allow
        assert "user denied" in verdict.reason

    @pytest.mark.asyncio
    async def test_browser_denied_in_plan(self) -> None:
        pipeline = PermissionsPipeline()
        tc = ToolCall(
            id="1", name="browser", arguments={"action": "navigate", "url": "https://example.com"}
        )
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert not verdict.allow

    # ── Day 30 tool permissions ──

    @pytest.mark.asyncio
    async def test_request_user_input_bypass(self) -> None:
        pipeline = PermissionsPipeline(bypass_tools={"request_user_input"})
        tc = ToolCall(id="1", name="request_user_input", arguments={"reason": "need info"})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow
        assert not verdict.ask_user

    @pytest.mark.asyncio
    async def test_todo_read_bypass_and_plan(self) -> None:
        pipeline = PermissionsPipeline(bypass_tools={"todo_read"})
        tc = ToolCall(id="1", name="todo_read", arguments={})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert verdict.allow

    @pytest.mark.asyncio
    async def test_todo_write_bypass_and_allowed_in_plan(self) -> None:
        # ——plan 模式放行；写盘类工具仍拒（见 pipeline_middleware 契约）
        pipeline = PermissionsPipeline(bypass_tools={"todo_write"})
        tc = ToolCall(id="1", name="todo_write", arguments={"todos": [{"title": "x"}]})
        verdict = await pipeline.check(tc, tool=None, mode="execute")
        assert verdict.allow
        verdict = await pipeline.check(tc, tool=None, mode="plan")
        assert verdict.allow


class MockUserInteraction:
    def __init__(self, allow: bool = True):
        self._allow = allow
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        self.calls.append((tool_name, args, risk))
        return self._allow


class TestConfirmCache:
    """深度复审修复：WARN 命令确认后同命令不再重复询问（双重询问 UX）。"""

    @pytest.mark.asyncio
    async def test_same_command_confirmed_once(self) -> None:
        ui = MockUserInteraction(allow=True)
        pipeline = PermissionsPipeline(user_interaction=ui)
        ok1 = await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push origin main"}, risk="medium"
        )
        ok2 = await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push origin main"}, risk="medium"
        )
        assert ok1 is True and ok2 is True
        assert len(ui.calls) == 1, "同命令确认一次后不得再次询问（双重询问修复）"

    @pytest.mark.asyncio
    async def test_different_command_asks_again(self) -> None:
        ui = MockUserInteraction(allow=True)
        pipeline = PermissionsPipeline(user_interaction=ui)
        await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push a"}, risk="medium"
        )
        await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push b"}, risk="medium"
        )
        assert len(ui.calls) == 2, "不同命令必须分别确认"

    @pytest.mark.asyncio
    async def test_denied_command_not_cached(self) -> None:
        ui = MockUserInteraction(allow=False)
        pipeline = PermissionsPipeline(user_interaction=ui)
        ok1 = await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push x"}, risk="medium"
        )
        ok2 = await pipeline.confirm_tool(
            tool_name="bash", arguments={"command": "git push x"}, risk="medium"
        )
        assert ok1 is False and ok2 is False
        assert len(ui.calls) == 2, "拒绝的命令不得缓存（下次仍可确认）"
