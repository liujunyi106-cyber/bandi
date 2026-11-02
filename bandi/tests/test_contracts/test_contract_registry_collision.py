"""
契约：ToolRegistry 同名注册碰撞 fail-closed —— 低危 「registry 同名静默覆盖」。

行为族（全部生产调用路径）：
- ToolRegistry.register（mcp_client.register_mcp_tools 装配面）
- ToolRegistry.register_instance（factory.build_tool_registry 装配面）
契约钉「行为结果」：不同工具对象同名注册 → ValueError；同对象幂等注册 → 放行；
MCP 重连先清后注（不会因名称残留而误伤/静默覆盖）→ 我方既有一致性。
"""

from typing import Any

import pytest

from bandi.tools.base import BaseTool
from bandi.tools.registry import ToolRegistry


class _Tool(BaseTool):
    name = "dupe_probe"
    description = "碰撞测试工具"
    enabled = True
    tags = ["util"]

    async def execute(self, params: dict, context: Any = None) -> str:
        return "ok"


def test_register_instance_duplicate_name_fail_closed() -> None:
    """register_instance：不同对象同名注册必须失败（防静默覆盖 registry.py:105）。"""
    registry = ToolRegistry()
    registry.register_instance(_Tool())
    with pytest.raises(ValueError):
        registry.register_instance(_Tool())


def test_register_duplicate_name_fail_closed() -> None:
    """register：不同对象同名注册必须失败（防静默覆盖 registry.py:98）。"""
    registry = ToolRegistry()
    registry.register(_Tool)
    with pytest.raises(ValueError):
        registry.register(_Tool)


def test_same_instance_re_register_idempotent() -> None:
    """同对象重复注册幂等放行——MCP 重连/装配复用不得因自身重挂而炸。"""
    registry = ToolRegistry()
    tool = _Tool()
    registry.register_instance(tool)
    registry.register_instance(tool)
    assert registry.get("dupe_probe") is not None
    assert len(registry.list_tools()) == 1


def test_register_mcp_tools_reconnect_replaces_cleanly() -> None:
    """MCP 重连：同一 client 重复 register_mcp_tools 先清后注，不残留旧工具
    不误伤其他 client 工具（原实现 register 直接覆盖=静默替换）。"""
    import asyncio

    from bandi.tools.mcp_client import register_mcp_tools

    class _FakeClient:
        name = "fake_mcp"
        available = True

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "fetch",
                    "description": "fetch page",
                    "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
            ]

    async def _run() -> list[str]:
        registry = ToolRegistry()
        client1 = _FakeClient()
        await register_mcp_tools(registry, client1)
        names_after_first = [t.name for t in registry.list_tools()]
        # 重连 = 同一名字的新 client 实例，再次注册同一批工具
        client2 = _FakeClient()
        await register_mcp_tools(registry, client2)
        names_after_reconnect = [t.name for t in registry.list_tools()]
        return names_after_first, names_after_reconnect

    first, reconnect = asyncio.run(_run())
    assert first == ["fake_mcp_fetch"]
    assert reconnect == ["fake_mcp_fetch"], "重连后不得残留/累积旧注册"


def _make_tool(name: str) -> BaseTool:
    # 功能：生成注册探测工具，named 构造，模拟 MCP 动态加载的可执行工具
    return type(f"MCPTool_{name.replace('-', '_')}", (_Tool,), {"name": name})()


@pytest.mark.parametrize(
    ("prefix", "other"), [("fs", "fs_backup"), ("git", "git_ops"), ("mcp", "mcp_tools")]
)
def test_unregister_prefix_never_removes_other_prefix(prefix: str, other: str) -> None:
    """契约：unregister_prefix('fs') 不得误删 fs_backup_*（子串前缀碰撞，registry.py:121
    旧实现 `startswith(f'{prefix}_')` 会把互为前缀 client 的工具一起删掉且不恢复）。

    互为前缀的命名（fs/fs_backup、git/git_ops、mcp/mcp_tools）是真实 MCP client 形态——
    前者重连清理（先清后注）时，后者的工具必须原样保留；误删 = 静默缺失功能。"""
    registry = ToolRegistry()
    registry.register_instance(_make_tool(f"{prefix}_read"), prefix=prefix)
    registry.register_instance(_make_tool(f"{other}_read"), prefix=other)
    registry.unregister_prefix(prefix)
    remaining = {t.name for t in registry.list_tools()}
    assert f"{prefix}_read" not in remaining, "本 namespace 工具应被清掉"
    assert f"{other}_read" in remaining, (
        f"unregister_prefix({prefix!r}) 误删了其他 client {other}_read——"
        "前缀清理必须精确到注册记账的 namespace，禁止子串匹配"
    )


def test_unregister_prefix_only_removes_prefix_registered() -> None:
    """无前缀记账的工具不得被任何 namespace 清理误删（隔离注册/共享注册互不串扰）。"""
    registry = ToolRegistry()
    registry.register_instance(_make_tool("standalone"))
    registry.register_instance(_make_tool("fs_read"), prefix="fs")
    registry.unregister_prefix("fs")
    assert registry.get("standalone") is not None, "无前缀工具被误删"
    assert registry.get("fs_read") is None


def test_unregister_prefix_unknown_ns_is_noop() -> None:
    """清理不存在的 namespace 必须无副作用（重连首跑的合法路径）。"""
    registry = ToolRegistry()
    registry.register_instance(_make_tool("fs_read"), prefix="fs")
    registry.unregister_prefix("never_connected")
    assert registry.get("fs_read") is not None


def test_extension_tools_sorted_after_builtin_segment() -> None:
    """契约：两段式排序——内建段（无 prefix 注册）按
    name 升序在前；namespace 扩展段（MCP 等 prefix 注册）按 name 升序在后。
    迟到注册的扩展工具不得使内建段位移（provider 前缀缓存对内建段保持稳定）。"""
    registry = ToolRegistry()
    registry.register_instance(_make_tool("bbb_builtin"))
    registry.register_instance(_make_tool("ddd_builtin"))
    registry.register_instance(_make_tool("ccc_mcp"), prefix="mcp")

    names = [t.name for t in registry.list_tools()]

    assert names == ["bbb_builtin", "ddd_builtin", "ccc_mcp"], (
        f"扩展段必须整体位于内建段之后（字母序不得跨段插入，实际 {names}）"
    )


def test_extension_segment_internal_order_by_name() -> None:
    """契约（对照）：扩展段内部同样按 name 升序（段间有序、段内有序）。"""
    registry = ToolRegistry()
    registry.register_instance(_make_tool("z_tool"), prefix="mcp")
    registry.register_instance(_make_tool("a_tool"), prefix="mcp")
    registry.register_instance(_make_tool("m_builtin"))

    names = [t.name for t in registry.list_tools()]

    assert names == ["m_builtin", "a_tool", "z_tool"], f"实际 {names}"
