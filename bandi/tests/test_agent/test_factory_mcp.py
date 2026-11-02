from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.agent.factory import build_agent_context, build_mcp_broker, build_tool_pipeline
from bandi.core.config import BandiConfig, MCPConfig
from bandi.tools.middleware import RetryMiddleware
from bandi.tools.pipeline import ToolPipeline


class TestBuildMcpBrokerDefault:
    @pytest.mark.asyncio
    async def test_no_servers_returns_none(self) -> None:
        cfg = BandiConfig()
        cfg.tools.mcp_servers = []
        broker = await build_mcp_broker(cfg)
        assert broker is None

    @pytest.mark.asyncio
    async def test_no_servers_no_pipeline_middleware(self) -> None:
        pipeline = build_tool_pipeline(mcp_broker=None)
        mw_types = [type(m) for m in pipeline._middlewares]
        assert RetryMiddleware in mw_types or len(pipeline._middlewares) == 0


class TestBuildMcpBrokerWithServers:
    @pytest.mark.asyncio
    async def test_servers_builds_broker_and_connects(self) -> None:
        cfg = BandiConfig()
        cfg.tools.mcp_servers = [
            MCPConfig(name="fs", pool="filesystem", transport="stdio", command="echo", args=[])
        ]
        mock_client = MagicMock()
        mock_client.available = True

        with (
            patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
            patch("bandi.tools.mcp_client.StdioTransport") as mock_transport,
        ):
            broker = await build_mcp_broker(cfg)
        assert broker is not None
        mock_transport.assert_called_once_with("echo", [])

    @pytest.mark.asyncio
    async def test_connect_failure_is_tolerated(self) -> None:
        """单个 server 连接失败不阻断 broker 构建（connect_all 捕获异常）。"""
        cfg = BandiConfig()
        cfg.tools.mcp_servers = [
            MCPConfig(name="x", transport="streamable_http", command="", args=[], url="")
        ]
        broker = await build_mcp_broker(cfg)
        assert broker is not None
        assert broker.pool_names == ["x"]

    @pytest.mark.asyncio
    async def test_health_check_started(self) -> None:
        cfg = BandiConfig()
        cfg.tools.mcp_servers = [
            MCPConfig(name="fs", pool="filesystem", transport="stdio", command="echo", args=[])
        ]
        mock_client = MagicMock()
        mock_client.available = True
        with (
            patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
            patch("bandi.tools.mcp_client.StdioTransport"),
        ):
            broker = await build_mcp_broker(cfg)
        assert broker is not None
        assert broker._health_task is not None or broker._running


class TestPipelineMCPOrder:
    @pytest.mark.asyncio
    async def test_retry_middleware_carrying_broker(self) -> None:
        """broker 参数传入 RetryMiddleware 单层，不再有独立 MCP 层。"""
        pipeline = build_tool_pipeline(mcp_broker=object())
        mw_types = [type(m) for m in pipeline._middlewares]
        assert RetryMiddleware in mw_types
        retry_mw = next(m for m in pipeline._middlewares if isinstance(m, RetryMiddleware))
        assert retry_mw._mcp_broker is not None

    def test_agent_context_accepts_mcp_broker(self) -> None:
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        broker = object()
        ctx = build_agent_context(user_input="test", bandi_config=cfg, mcp_broker=broker)
        assert ctx.tool_executor is not None
        pipeline: ToolPipeline = ctx.tool_executor._pipeline
        assert pipeline is not None
        mw_types = [type(m) for m in pipeline._middlewares]
        assert RetryMiddleware in mw_types


class TestMcpBrokerPrefix:
    @pytest.mark.asyncio
    async def test_broker_register_prefix_used(self) -> None:
        from bandi.tools.mcp_broker import MCPBroker

        broker = MCPBroker()
        client = MagicMock()
        client.available = True
        broker.register(client, pool_name="filesystem", prefix="tools")
        with patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg:
            await broker.register_all_tools(MagicMock())
        args, kwargs = mock_reg.await_args
        assert kwargs.get("prefix") == "tools"

    @pytest.mark.asyncio
    async def test_broker_register_prefix_none_falls_back_to_pool(self) -> None:
        from bandi.tools.mcp_broker import MCPBroker

        broker = MCPBroker()
        client = MagicMock()
        client.available = True
        broker.register(client, pool_name="filesystem", prefix=None)
        with patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg:
            await broker.register_all_tools(MagicMock())
        args, kwargs = mock_reg.await_args
        assert kwargs.get("prefix") == "filesystem"

    @pytest.mark.asyncio
    async def test_build_mcp_broker_forwards_server_prefix(self) -> None:
        cfg = BandiConfig()
        cfg.tools.mcp_servers = [
            MCPConfig(
                name="fs",
                pool="filesystem",
                prefix="tools",
                transport="stdio",
                command="echo",
                args=[],
            )
        ]
        mock_client = MagicMock()
        mock_client.available = True
        mock_client.connect = AsyncMock()
        with (
            patch("bandi.tools.mcp_client.MCPClient", return_value=mock_client),
            patch("bandi.tools.mcp_client.StdioTransport"),
            patch("bandi.tools.mcp_broker.register_mcp_tools", new_callable=AsyncMock) as mock_reg,
        ):
            broker = await build_mcp_broker(cfg, registry=MagicMock())
        assert broker is not None
        mock_reg.assert_awaited()
        args, kwargs = mock_reg.await_args
        assert kwargs.get("prefix") == "tools"

    @pytest.mark.asyncio
    async def test_prefix_controls_registered_tool_names(self) -> None:
        """最终产物断言：prefix 必须实际作用于注册到 registry 的工具名（生产主链路）。"""
        from bandi.tools.mcp_broker import MCPBroker
        from bandi.tools.registry import ToolRegistry

        broker = MCPBroker()
        client = MagicMock()
        client.available = True
        client.list_tools = AsyncMock(
            return_value=[
                {"name": "read_file", "description": "read", "inputSchema": {"type": "object"}}
            ]
        )
        broker.register(client, pool_name="filesystem", prefix="tools")
        registry = ToolRegistry()
        await broker.register_all_tools(registry)
        assert registry.get("tools_read_file") is not None
        assert registry.get("filesystem_read_file") is None
