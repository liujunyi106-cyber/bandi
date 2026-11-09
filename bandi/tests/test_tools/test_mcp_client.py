import asyncio

import pytest

from bandi.tools.mcp_client import MCPClient, MCPError, StdioTransport, register_mcp_tools
from bandi.tools.registry import ToolRegistry


class MockStdioTransport(StdioTransport):
    def __init__(self):
        super().__init__("echo", [])
        self._mock_responses: dict[str, list[dict]] = {}
        self._connected = False
        self._sent: list[dict] = []

    def add_response(self, method: str, response: dict) -> None:
        self._mock_responses.setdefault(method, []).append(response)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        self._sent.append({"method": method, "params": params})
        responses = self._mock_responses.get(method, [])
        if responses:
            return responses.pop(0)
        return {}

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        self._sent.append({"method": method, "params": params})

    @property
    def available(self) -> bool:
        return self._connected


@pytest.mark.asyncio
async def test_mcp_client_connect_and_list_tools():
    transport = MockStdioTransport()
    transport.add_response("initialize", {"serverInfo": {"name": "test-server"}})
    transport.add_response(
        "tools/list",
        {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo input",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        },
    )

    client = MCPClient("test", transport)
    await client.connect()

    assert client.available
    tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "echo"


@pytest.mark.asyncio
async def test_mcp_client_call_tool():
    transport = MockStdioTransport()
    transport.add_response("initialize", {"serverInfo": {"name": "test-server"}})
    transport.add_response(
        "tools/call",
        {"content": [{"type": "text", "text": "hello back"}]},
    )

    client = MCPClient("test", transport)
    await client.connect()

    result = await client.call_tool("echo", {"text": "hello"})
    assert result["content"][0]["text"] == "hello back"


@pytest.mark.asyncio
async def test_mcp_client_reconnect():
    transport = MockStdioTransport()
    transport.add_response("initialize", {"serverInfo": {"name": "test-server"}})
    transport.add_response("initialize", {"serverInfo": {"name": "test-server"}})

    client = MCPClient("test", transport)
    await client.connect()
    assert client.available

    transport._connected = False
    assert not client.available

    result = await client.reconnect(max_retries=2)
    assert result
    assert client.available


@pytest.mark.asyncio
async def test_register_mcp_tools():
    registry = ToolRegistry()
    transport = MockStdioTransport()
    transport.add_response("initialize", {"serverInfo": {"name": "test-server"}})
    transport.add_response(
        "tools/list",
        {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo test",
                    "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}},
                }
            ]
        },
    )

    client = MCPClient("my-server", transport)
    await client.connect()

    registered = await register_mcp_tools(registry, client)
    assert len(registered) == 1
    assert registered[0] == "my-server_echo"

    tool = registry.get("my-server_echo")
    assert tool is not None
    assert tool.description == "Echo test [来源: mcp:my-server]"
    assert tool.parameters == {"type": "object", "properties": {"msg": {"type": "string"}}}


@pytest.mark.asyncio
async def test_register_mcp_tools_skips_unavailable():
    registry = ToolRegistry()
    transport = MockStdioTransport()

    client = MCPClient("offline", transport)
    registered = await register_mcp_tools(registry, client)
    assert registered == []


def test_mcp_error():
    err = MCPError("connection failed")
    assert str(err) == "connection failed"


@pytest.mark.asyncio
async def test_stdio_disconnect_fails_pending_requests():
    """disconnect 必须让进行中的 send_request 立即失败（不得悬挂到超时）。"""
    from unittest.mock import AsyncMock, MagicMock

    transport = StdioTransport("echo", [])
    transport._process = MagicMock()
    transport._process.returncode = None
    transport._process.kill = MagicMock()
    transport._process.wait = AsyncMock(return_value=0)
    transport._writer = MagicMock()
    transport._reader = MagicMock()
    transport._reader.at_eof = MagicMock(return_value=True)
    transport._read_task = None

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    transport._pending[1] = fut

    await transport.disconnect()

    assert fut.done(), "disconnect 后 pending future 必须立即完成"
    with pytest.raises(MCPError, match="transport disconnected"):
        fut.result()
    assert transport._pending == {}, "pending 必须清空"


@pytest.mark.asyncio
async def test_mcp_client_available_property():
    transport = MockStdioTransport()
    transport.add_response("initialize", {"serverInfo": {"name": "test"}})
    client = MCPClient("test", transport)
    assert not client.available

    await client.connect()
    assert client.available

    await client.disconnect()


@pytest.mark.asyncio
async def test_broker_per_client_prefix_not_overwritten():
    """
    被后注册覆盖，两工具共享同一 prefix。"""
    from bandi.tools.mcp_broker import MCPBroker

    def _tool(name: str) -> dict:
        return {
            "name": name,
            "description": "d",
            "inputSchema": {"type": "object", "properties": {}},
        }

    transport_a = MockStdioTransport()
    transport_a.add_response("initialize", {"serverInfo": {"name": "svc-a"}})
    transport_a.add_response("tools/list", {"tools": [_tool("alpha")]})
    transport_b = MockStdioTransport()
    transport_b.add_response("initialize", {"serverInfo": {"name": "svc-b"}})
    transport_b.add_response("tools/list", {"tools": [_tool("beta")]})

    client_a = MCPClient("svc-a", transport_a)
    client_b = MCPClient("svc-b", transport_b)
    broker = MCPBroker()
    broker.register(client_a, pool_name="pool", prefix="pa")
    broker.register(client_b, pool_name="pool", prefix="pb")
    await broker.connect_all()
    registry = ToolRegistry()
    await broker.register_all_tools(registry)
    names = {t.name for t in registry.list_tools()}
    # 同池多 client：prefix 后接 server 名消歧，各自保留自己的前缀
    assert "pa__svc_a_alpha" in names, f"client A 的 prefix 被覆盖: {names}"
    assert "pb__svc_b_beta" in names, f"client B 的 prefix 被覆盖: {names}"
    assert not any(name.startswith("pool") for name in names), "不得退化为 pool 名作为 prefix"


@pytest.mark.asyncio
async def test_contract_read_loop_exit_fails_pending_immediately():
    """
    连接同时标记为不可用（broker 停止路由新请求）。"""
    from types import SimpleNamespace

    from bandi.tools.mcp_client import MCPError, StdioTransport

    t = StdioTransport("noop")
    reader = asyncio.StreamReader()
    reader.feed_eof()
    t._reader = reader
    t._writer = None
    t._process = SimpleNamespace(returncode=None)  # 模拟进程尚未被 reap

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    t._pending[1] = fut

    await asyncio.wait_for(asyncio.create_task(t._read_loop()), timeout=5)

    assert fut.done(), "读循环退出后 pending 未被失败"
    assert isinstance(fut.exception(), MCPError)
    assert not t.available, "读循环退出后连接仍标记可用（broker 会继续路由死连接）"


@pytest.mark.asyncio
async def test_contract_disconnect_fail_pending_reuses_helper():
    """
    双路径语义一致且幂等。"""
    from types import SimpleNamespace

    from bandi.tools.mcp_client import MCPError, StdioTransport

    t = StdioTransport("noop")
    t._process = SimpleNamespace(returncode=0)  # 已退出进程
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    t._pending[7] = fut

    await t.disconnect()

    assert fut.done() and isinstance(fut.exception(), MCPError)
    assert t._pending == {}
