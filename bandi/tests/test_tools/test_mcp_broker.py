import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import MCPClient, StdioTransport
from bandi.tools.pipeline import ToolContext


class MockBrokerTransport(StdioTransport):
    def __init__(self):
        super().__init__("echo", [])
        self._connections = 0
        self._disconnections = 0
        self._connected = False
        self._responses: dict[str, list[dict]] = {}
        self._sent: list[dict] = []

    def add_response(self, method: str, response: dict) -> None:
        self._responses.setdefault(method, []).append(response)

    async def connect(self) -> None:
        self._connections += 1
        self._connected = True

    async def disconnect(self) -> None:
        self._disconnections += 1
        self._connected = False

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        self._sent.append({"method": method, "params": params})
        responses = self._responses.get(method, [])
        if responses:
            return responses.pop(0)
        return {}

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        self._sent.append({"method": method, "params": params})

    @property
    def available(self) -> bool:
        return self._connected


@pytest.mark.asyncio
async def test_broker_register_and_pool_names():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    t2 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    c2 = MCPClient("fs-2", t2)

    broker.register(c1, pool_name="filesystem")
    broker.register(c2, pool_name="filesystem")

    assert broker.pool_names == ["filesystem"]
    assert broker.total_clients == 2


@pytest.mark.asyncio
async def test_broker_connect_all():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    broker.register(c1, pool_name="filesystem")

    errors = await broker.connect_all()
    assert errors == {}
    assert t1._connections == 1
    assert c1.available


@pytest.mark.asyncio
async def test_broker_disconnect_all():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    broker.register(c1, pool_name="filesystem")
    await broker.connect_all()
    assert c1.available

    await broker.disconnect_all()
    assert not c1.available
    assert t1._disconnections == 1


@pytest.mark.asyncio
async def test_broker_get_client_round_robin():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    t2 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    c2 = MCPClient("fs-2", t2)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    t2.add_response("initialize", {"serverInfo": {"name": "fs-2"}})

    broker.register(c1, pool_name="filesystem")
    broker.register(c2, pool_name="filesystem")
    await broker.connect_all()

    got1 = broker.get_client("filesystem")
    got2 = broker.get_client("filesystem")
    got3 = broker.get_client("filesystem")

    assert got1 is not None
    assert got2 is not None
    assert got3 is not None
    # Round-robin should cycle through both clients
    ids = {got1.name, got2.name, got3.name}
    assert len(ids) >= 2  # at least 2 unique clients visited


@pytest.mark.asyncio
async def test_broker_get_client_empty_pool():
    broker = MCPBroker()
    assert broker.get_client("nonexistent") is None


@pytest.mark.asyncio
async def test_broker_get_client_all_unavailable():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    broker.register(c1, pool_name="filesystem")
    # connect then disconnect so it's unavailable
    await broker.connect_all()
    await broker.disconnect_all()

    assert broker.get_client("filesystem") is None


@pytest.mark.asyncio
async def test_broker_call_tool():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    t1.add_response(
        "tools/call",
        {"content": [{"type": "text", "text": "result"}]},
    )
    broker.register(c1, pool_name="filesystem")
    await broker.connect_all()

    result = await broker.call_tool("filesystem", "read", {"path": "/tmp"})
    assert result["content"][0]["text"] == "result"


@pytest.mark.asyncio
async def test_broker_call_tool_no_client():
    broker = MCPBroker()
    with pytest.raises(Exception, match="no available client"):
        await broker.call_tool("nonexistent", "tool", {})


@pytest.mark.asyncio
async def test_broker_register_all_tools():
    from bandi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    t1.add_response(
        "tools/list",
        {
            "tools": [
                {
                    "name": "read",
                    "description": "Read file",
                    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                }
            ]
        },
    )
    broker.register(c1, pool_name="filesystem")
    await broker.connect_all()

    results = await broker.register_all_tools(registry)
    assert "filesystem" in results
    assert len(results["filesystem"]) == 1
    assert results["filesystem"][0] == "filesystem_read"

    tool = registry.get("filesystem_read")
    assert tool is not None
    assert tool.name == "filesystem_read"


@pytest.mark.asyncio
async def test_broker_same_pool_two_clients_do_not_overwrite():
    """同 pool 多 client 必须使用独立 namespace，后注册者不得清掉前者工具。"""
    from bandi.tools.registry import ToolRegistry

    def _client(name: str, tool_name: str) -> tuple[MCPClient, MockBrokerTransport]:
        transport = MockBrokerTransport()
        transport.add_response("initialize", {"serverInfo": {"name": name}})
        transport.add_response(
            "tools/list",
            {
                "tools": [
                    {
                        "name": tool_name,
                        "description": tool_name,
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        )
        return MCPClient(name, transport), transport

    registry = ToolRegistry()
    broker = MCPBroker()
    c1, _ = _client("fs-1", "read")
    c2, _ = _client("fs-2", "write")
    broker.register(c1, pool_name="filesystem")
    broker.register(c2, pool_name="filesystem")
    await broker.connect_all()

    results = await broker.register_all_tools(registry)

    assert "filesystem__fs_1_read" in results["filesystem"]
    assert "filesystem__fs_2_write" in results["filesystem"]
    assert registry.get("filesystem__fs_1_read") is not None
    assert registry.get("filesystem__fs_2_write") is not None


@pytest.mark.asyncio
async def test_broker_get_pool_summary():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    broker.register(c1, pool_name="filesystem")

    summary = broker.get_pool_summary()
    assert summary["filesystem"]["total"] == 1
    assert summary["filesystem"]["available"] == 0
    assert "fs-1" in summary["filesystem"]["clients"]

    await broker.connect_all()
    summary = broker.get_pool_summary()
    assert summary["filesystem"]["available"] == 1


@pytest.mark.asyncio
async def test_broker_unregister():
    broker = MCPBroker()
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    broker.register(c1, pool_name="filesystem")
    assert broker.total_clients == 1

    broker.unregister(c1)
    assert broker.total_clients == 0
    assert broker.pool_names == []


@pytest.mark.asyncio
async def test_broker_health_check_start_stop():
    broker = MCPBroker(health_check_interval=0.1)
    t1 = MockBrokerTransport()
    c1 = MCPClient("fs-1", t1)
    t1.add_response("initialize", {"serverInfo": {"name": "fs-1"}})
    broker.register(c1, pool_name="filesystem")
    await broker.connect_all()

    broker.start_health_check()
    assert broker._running
    assert broker._health_task is not None

    await broker.stop_health_check()
    assert not broker._running


@pytest.mark.asyncio
async def test_mcp_middleware_passthrough_on_success():
    broker = MCPBroker()
    from bandi.tools.middleware import RetryMiddleware

    mw = RetryMiddleware(max_retries=1, base_delay=0.0, mcp_broker=broker)
    ctx = ToolContext()
    call = ToolCall(id="1", name="test_tool", arguments={})
    ok_result = ToolResult(tool_call_id="1", tool_name="test_tool", success=True, output="ok")

    result = await mw.after_execute(ctx, call, ok_result)
    assert result.success
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_mcp_middleware_passthrough_non_mcp_error():
    broker = MCPBroker()
    from bandi.tools.middleware import RetryMiddleware

    mw = RetryMiddleware(max_retries=1, base_delay=0.0, mcp_broker=broker)
    ctx = ToolContext()
    call = ToolCall(id="1", name="bash", arguments={"cmd": "ls"})
    err_result = ToolResult(
        tool_call_id="1", tool_name="bash", success=False, output="", error="command not found"
    )

    result = await mw.after_execute(ctx, call, err_result)
    assert not result.success
    assert "command not found" in result.error


def test_mcp_connection_error_detection():
    broker = MCPBroker()
    from bandi.tools.middleware import RetryMiddleware

    mw = RetryMiddleware(max_retries=1, base_delay=0.0, mcp_broker=broker)

    assert mw._is_mcp_connection_error("MCP transport closed")
    assert mw._is_mcp_connection_error("connection refused")
    assert mw._is_mcp_connection_error("unavailable")
    assert mw._is_mcp_connection_error("transport error")
    assert not mw._is_mcp_connection_error("validation: invalid param")
    assert not mw._is_mcp_connection_error("")
    assert not mw._is_mcp_connection_error(None)
