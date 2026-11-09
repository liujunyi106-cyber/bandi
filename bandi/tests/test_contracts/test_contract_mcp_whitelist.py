from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.agent.factory import build_mcp_broker
from bandi.core.config import BandiConfig, MCPConfig
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import MCPClient, StdioTransport
from bandi.tools.registry import ToolRegistry


class _FakeTransport(StdioTransport):
    def __init__(self, tools: list[dict]) -> None:
        super().__init__("echo", [])
        self._tools = tools
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "tools/list":
            return {"tools": self._tools}
        return {}

    async def send_notification(self, method: str, params: dict | None = None) -> None: ...

    @property
    def available(self) -> bool:
        return self._connected


def _two_tools() -> list[dict]:
    return [
        {"name": "read", "description": "Read file", "inputSchema": {"type": "object"}},
        {"name": "write", "description": "Write file", "inputSchema": {"type": "object"}},
    ]


@pytest.mark.asyncio
async def test_broker_allowed_tools_filters_nonlisted() -> None:
    # 契约：配置白名单后，名单外的远端工具一律不注册（fail-closed）
    broker = MCPBroker()
    client = MCPClient("fs-1", _FakeTransport(_two_tools()))
    broker.register(client, pool_name="filesystem", allowed_tools={"filesystem_read"})
    await broker.connect_all()
    registry = ToolRegistry()
    results = await broker.register_all_tools(registry)

    assert results["filesystem"] == ["filesystem_read"]
    assert registry.get("filesystem_read") is not None
    assert registry.get("filesystem_write") is None


@pytest.mark.asyncio
async def test_broker_without_allowed_tools_registers_all() -> None:
    # 契约：程序化注册未带白名单 = 不受限（既有语义保留）
    broker = MCPBroker()
    client = MCPClient("fs-1", _FakeTransport(_two_tools()))
    broker.register(client, pool_name="filesystem")
    await broker.connect_all()
    registry = ToolRegistry()
    results = await broker.register_all_tools(registry)

    assert sorted(results["filesystem"]) == ["filesystem_read", "filesystem_write"]


@pytest.mark.asyncio
async def test_builder_forwards_config_allowed_tools() -> None:
    # 契约：工具前缀'build_mcp_broker'装配点把 config 白名单透传给 register_mcp_tools
    cfg = BandiConfig()
    cfg.tools.mcp_servers = [
        MCPConfig(
            name="fs",
            pool="filesystem",
            transport="stdio",
            command="echo",
            args=[],
            allowed_tools=["filesystem_read"],
        )
    ]
    mock_client = MagicMock()
    mock_client.available = True
    with (
        patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
        patch("bandi.tools.mcp_client.StdioTransport"),
        patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg,
    ):
        broker = await build_mcp_broker(cfg, registry=MagicMock())
    assert broker is not None
    mock_reg.assert_awaited()
    _, kwargs = mock_reg.await_args
    assert kwargs.get("allowed_tools") == {"filesystem_read"}


@pytest.mark.asyncio
async def test_builder_without_allowed_tools_passes_none() -> None:
    cfg = BandiConfig()
    cfg.tools.mcp_servers = [
        MCPConfig(name="fs", pool="filesystem", transport="stdio", command="echo", args=[])
    ]
    mock_client = MagicMock()
    mock_client.available = True
    with (
        patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
        patch("bandi.tools.mcp_client.StdioTransport"),
        patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg,
    ):
        broker = await build_mcp_broker(cfg, registry=MagicMock())
    assert broker is not None
    mock_reg.assert_awaited()
    _, kwargs = mock_reg.await_args
    assert kwargs.get("allowed_tools") is None


@pytest.mark.asyncio
async def test_builder_explicit_empty_allowed_tools_fail_closed() -> None:
    # 契约：显式配置空白名单（allowed_tools: []）= 全部拒绝，生产透传空集
    cfg = BandiConfig()
    cfg.tools.mcp_servers = [
        MCPConfig(
            name="fs",
            pool="filesystem",
            transport="stdio",
            command="echo",
            args=[],
            allowed_tools=[],
        )
    ]
    mock_client = MagicMock()
    mock_client.available = True
    with (
        patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
        patch("bandi.tools.mcp_client.StdioTransport"),
        patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg,
    ):
        broker = await build_mcp_broker(cfg, registry=MagicMock())
    assert broker is not None
    mock_reg.assert_awaited()
    _, kwargs = mock_reg.await_args
    assert kwargs.get("allowed_tools") == set()
