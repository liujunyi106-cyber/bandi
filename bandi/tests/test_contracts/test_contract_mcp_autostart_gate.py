# 功能：契约测试——MCP 默认不连接；显式开启才连接且进程级复用
from __future__ import annotations

import asyncio
from typing import Any

from bandi.core import agent_runner as runner
from bandi.core.config import BandiConfig, MCPConfig


def _config(*, autostart: bool, with_server: bool = True) -> BandiConfig:
    cfg = BandiConfig()
    servers = [MCPConfig(name="demo", transport="stdio", command="python")] if with_server else []
    return cfg.model_copy(
        update={
            "tools": cfg.tools.model_copy(
                update={"mcp_servers": servers, "mcp_autostart": autostart}
            )
        }
    )


class _FakeBroker:
    def __init__(self) -> None:
        self.register_calls = 0

    async def register_all_tools(self, registry: Any) -> None:
        self.register_calls += 1


class TestMCPAutostartGate:
    def setup_method(self) -> None:
        runner._reset_mcp_broker_cache()

    def test_explicit_disabled_no_connect(self, monkeypatch) -> None:
        """显式关闭：有 servers 配置也不连接、不拉起子进程。"""
        calls: list[int] = []

        async def fake_build(cfg: BandiConfig, registry: Any) -> Any:
            calls.append(1)
            return _FakeBroker()

        monkeypatch.setattr(runner, "build_mcp_broker", fake_build)
        broker = asyncio.run(runner._acquire_mcp_broker(_config(autostart=False), None))
        assert broker is None
        assert calls == [], "显式关闭时不得触发 MCP 连接"

    def test_default_enabled_connects(self, monkeypatch) -> None:
        """默认开启：有 servers 配置时建立连接，工具注册后由懒暴露控制 schema。"""
        calls: list[int] = []

        async def fake_build(cfg: BandiConfig, registry: Any) -> Any:
            calls.append(1)
            return _FakeBroker()

        monkeypatch.setattr(runner, "build_mcp_broker", fake_build)
        broker = asyncio.run(runner._acquire_mcp_broker(_config(autostart=True), None))
        assert broker is not None
        assert calls == [1]

    def test_enabled_connects_and_reuses_cache(self, monkeypatch) -> None:
        """开启后：首次连接，后续 run 复用同一 broker（不重复拉子进程）。"""
        from bandi.tools.registry import ToolRegistry

        built: list[int] = []
        broker = _FakeBroker()

        async def fake_build(cfg: BandiConfig, registry: Any) -> Any:
            built.append(1)
            return broker

        monkeypatch.setattr(runner, "build_mcp_broker", fake_build)
        monkeypatch.setattr(
            "bandi.tools.mcp_resources.register_mcp_resource_tools", lambda reg, b: None
        )
        cfg = _config(autostart=True)
        first = asyncio.run(runner._acquire_mcp_broker(cfg, ToolRegistry()))
        second = asyncio.run(runner._acquire_mcp_broker(cfg, ToolRegistry()))
        assert first is broker and second is broker
        assert len(built) == 1, "同一配置只允许连接一次"
        assert broker.register_calls == 1, "复用轮须把工具注册到当轮 registry"

    def test_enabled_without_servers_stays_none(self, monkeypatch) -> None:
        async def fake_build(cfg: BandiConfig, registry: Any) -> Any:
            raise AssertionError("无 servers 不应触发连接")

        monkeypatch.setattr(runner, "build_mcp_broker", fake_build)
        broker = asyncio.run(
            runner._acquire_mcp_broker(_config(autostart=True, with_server=False), None)
        )
        assert broker is None

    def test_records_mcp_tool_names_for_surface(self, monkeypatch) -> None:
        """开启时把 MCP 工具名记到 broker（供工具面 union，不被相关性裁剪掉）。"""
        from bandi.tools.base import BaseTool, ToolCategory
        from bandi.tools.registry import ToolRegistry

        class _Registered(BaseTool):
            name: str = "demo_mcp_tool"
            description: str = "MCP 工具"
            category: ToolCategory = ToolCategory.INTEGRATION

            async def execute(self, params, context=None):  # type: ignore[no-untyped-def]
                return "ok"

        registry = ToolRegistry()

        class _BrokerWithTool:
            mcp_tool_names: list[str] = []

            async def register_all_tools(self, reg: Any) -> None:
                reg.register_instance(_Registered())

        async def fake_build(cfg: BandiConfig, reg: Any) -> Any:
            broker = _BrokerWithTool()
            await broker.register_all_tools(reg)
            return broker

        monkeypatch.setattr(runner, "build_mcp_broker", fake_build)
        broker = asyncio.run(runner._acquire_mcp_broker(_config(autostart=True), registry))
        assert broker is not None
        assert "demo_mcp_tool" in broker.mcp_tool_names
