"""契约族：员工工具绑定的执行面门禁 + 子代理工具面预算。

背景（2026-11-08）：
- 员工档案可绑定具体工具（`resource_type=tool`），但此前只用于收窄 schema，
  **执行期没有校验**——模型凭空点名绑定外工具仍会执行，属越权面。
- 子代理的工具面此前只做角色过滤，绕过主链「超过 35 个才裁」的预算控制。

本契约钉两条承诺：
- 绑定非空时，未绑定的工具调用被拒（交互/协作通道恒豁免）；
- 子代理 schema 面与主链同源裁剪，超过 MAX_TOOLS 时按任务描述收敛。
"""

from __future__ import annotations

from typing import Any

from bandi.agent.subagent import _subagent_schema_allowlist
from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from bandi.tools.selection import MAX_TOOLS


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tags: list[str] = []
        self.category = None


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def list_tools(self) -> list[_FakeTool]:
        return [_FakeTool(n) for n in self._names]

    def list_schemas(self, allowed_tools: list[str] | None = None) -> list[dict[str, Any]]:
        allow = set(allowed_tools) if allowed_tools is not None else None
        return [
            {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
            for n in self._names
            if allow is None or n in allow
        ]

    def get(self, name: str) -> Any:
        return None


def test_bound_tool_outside_allowlist_is_denied_at_execution() -> None:
    """破坏者：模型点名未绑定工具 → 执行门禁必须拒绝，不能只靠 schema 不可见。"""
    ex = ToolExecutor(_FakeRegistry(["read", "bash"]), bound_tool_names=["read"])
    denial = ex._role_gate(ToolCall(id="c1", name="bash", arguments={}))
    assert denial is not None
    assert denial.success is False
    assert "not bound" in (denial.error or "")


def test_bound_tool_inside_allowlist_passes_gate() -> None:
    ex = ToolExecutor(_FakeRegistry(["read", "bash"]), bound_tool_names=["read"])
    assert ex._role_gate(ToolCall(id="c2", name="read", arguments={})) is None


def test_unbound_config_does_not_restrict() -> None:
    """空/None 绑定 = 未配置 = 不收窄（与 schema 面 opt-in 语义一致）。"""
    ex = ToolExecutor(_FakeRegistry(["read", "bash"]), bound_tool_names=None)
    assert ex._role_gate(ToolCall(id="c3", name="bash", arguments={})) is None


def test_interactive_channel_exempt_from_binding() -> None:
    """互动通道恒豁免：否则绑定了工具的员工无法向用户提问。"""
    ex = ToolExecutor(_FakeRegistry(["read"]), bound_tool_names=["read"])
    name = sorted(ToolRegistry.ALWAYS_AVAILABLE_TOOLS)[0]
    assert ex._role_gate(ToolCall(id="c4", name=name, arguments={})) is None


def test_subagent_schema_face_respects_tool_budget() -> None:
    """子代理未显式限定工具时也必须经过超量裁剪，不得整包注入。"""
    registry = _FakeRegistry([f"tool_{i}" for i in range(MAX_TOOLS * 2)])
    names = _subagent_schema_allowlist(None, registry, None, query="读取本地文件并汇总")
    assert len(names) <= MAX_TOOLS + len(ToolRegistry.ALWAYS_AVAILABLE_TOOLS)
    assert names, "裁剪后不得为空（含交互通道保底）"
