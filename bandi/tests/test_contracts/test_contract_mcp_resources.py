# 功能：MCP resource 契约族——resources/list + resources/read 全链（client→broker→工具→装配）
# 生产调用路径唯一链：build_mcp_broker 装配 → register_mcp_resource_tools → 工具.execute → broker.{list,read}_resource → client.{list,read}_resource → transport
# 行为族级覆盖（DoD-10）：过滤/聚合/失败容忍/能力判断/blob 落盘白名单/上限/空结果/装配&门控，逐层钉行为不钉实现
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import MCPClient, MCPError, MCPTransport
from bandi.tools.registry import ToolRegistry


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute")


class _MockTransport(MCPTransport):
    """功能协议 mock：按 method 排队响应，记录已发帧（契约断言请求构造）。"""

    def __init__(self) -> None:
        self._responses: dict[str, list[dict[str, Any]]] = {}
        self._sent: list[dict[str, Any]] = []
        self._connected = False

    def queue(self, method: str, response: dict[str, Any]) -> None:
        self._responses.setdefault(method, []).append(response)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        self._sent.append({"method": method, "params": params})

    async def send_request(self, method: str, params: dict | None = None) -> dict[str, Any]:
        self._sent.append({"method": method, "params": params})
        queue = self._responses.get(method) or []
        return queue.pop(0) if queue else {}

    @property
    def available(self) -> bool:
        return self._connected


def _connected_client(
    name: str,
    *,
    resources: list[dict[str, Any]] | None = None,
    contents: list[dict[str, Any]] | None = None,
    capabilities_resources: bool = True,
) -> tuple[MCPClient, _MockTransport]:
    transport = _MockTransport()
    caps = {"resources": {"subscribe": False}} if capabilities_resources else {}
    transport.queue("initialize", {"serverInfo": {"name": name}, "capabilities": caps})
    transport.queue("resources/list", {"resources": resources or []})
    if contents is not None:
        transport.queue("resources/read", {"contents": contents})
    client = MCPClient(name, transport)
    return client, transport


# ---------- 层 1：MCPClient 传输协议契约 ----------


@pytest.mark.asyncio
async def test_client_list_resources_sends_method_and_parses():
    client, transport = _connected_client("svc", resources=[{"uri": "file://a", "name": "A"}])
    await client.connect()
    resources = await client.list_resources()
    sent = transport._sent[-1]
    assert sent["method"] == "resources/list"
    assert resources == [{"uri": "file://a", "name": "A"}]


@pytest.mark.asyncio
async def test_client_list_resources_non_list_result_falls_back_empty():
    client, transport = _connected_client("svc")
    interp = await client.list_resources()
    # 先造一个非 list 响应：替换响应队列为空 → send_request 返回 {}，必须回落 []
    assert interp == []
    assert transport._sent[-1]["method"] == "resources/list"


@pytest.mark.asyncio
async def test_client_read_resource_sends_method_and_uri():
    client, transport = _connected_client("svc", contents=[{"uri": "file://a", "text": "hi"}])
    await client.connect()
    contents = await client.read_resource("file://a")
    sent = transport._sent[-1]
    assert sent["method"] == "resources/read"
    assert sent["params"] == {"uri": "file://a"}
    assert contents == [{"uri": "file://a", "text": "hi"}]


# ---------- 层 2：MCPBroker 聚合契约 ----------


@pytest.mark.asyncio
async def test_broker_list_aggregates_across_clients_with_server_annotation():
    broker = MCPBroker()
    c1, _ = _connected_client("svc-a", resources=[{"uri": "file://a", "name": "A"}])
    c2, _ = _connected_client("svc-b", resources=[{"uri": "file://b", "name": "B"}])
    broker.register(c1, pool_name="pool-x")
    broker.register(c2, pool_name="pool-y")
    await broker.connect_all()
    result = await broker.list_resources()
    assert result == [
        {"uri": "file://a", "name": "A", "mimeType": None, "description": None, "server": "svc-a"},
        {"uri": "file://b", "name": "B", "mimeType": None, "description": None, "server": "svc-b"},
    ]


@pytest.mark.asyncio
async def test_broker_list_server_filter_matches_client_name():
    broker = MCPBroker()
    c1, _ = _connected_client("svc-a", resources=[{"uri": "file://a", "name": "A"}])
    c2, _ = _connected_client("svc-b", resources=[{"uri": "file://b", "name": "B"}])
    broker.register(c1, pool_name="pool-x")
    broker.register(c2, pool_name="pool-y")
    await broker.connect_all()
    result = await broker.list_resources(server="svc-a")
    assert [r["server"] for r in result] == ["svc-a"]
    assert len(result) == 1


@pytest.mark.asyncio
async def test_broker_list_unknown_server_raises_with_available_list():
    broker = MCPBroker()
    c1, _ = _connected_client("svc-a")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    with pytest.raises(MCPError) as exc_info:
        await broker.list_resources(server="nope")
    assert 'Server "nope" not found' in str(exc_info.value)
    assert "svc-a" in str(exc_info.value)


@pytest.mark.asyncio
async def test_broker_list_tolerates_single_client_failure():
    broker = MCPBroker()
    c1, _ = _connected_client("svc-broken", resources=None)
    c2, _ = _connected_client("svc-ok", resources=[{"uri": "file://ok", "name": "OK"}])
    broker.register(c1, pool_name="pool-x")
    broker.register(c2, pool_name="pool-y")
    await broker.connect_all()

    class _ExplodingError(MCPError):
        pass

    async def _boom() -> list[dict[str, Any]]:
        raise MCPError("connection reset")

    c1.list_resources = _boom  # type: ignore[method-assign]
    result = await broker.list_resources()
    assert [r["server"] for r in result] == ["svc-ok"]


@pytest.mark.asyncio
async def test_broker_list_skips_unavailable_client():
    broker = MCPBroker()
    c1, _ = _connected_client("svc-down", resources=[{"uri": "file://d", "name": "D"}])
    c2, _ = _connected_client("svc-up", resources=[{"uri": "file://u", "name": "U"}])
    broker.register(c1, pool_name="pool-x")
    broker.register(c2, pool_name="pool-y")
    await broker.connect_all()
    await c1.disconnect()
    result = await broker.list_resources()
    assert [r["server"] for r in result] == ["svc-up"]


@pytest.mark.asyncio
async def test_broker_read_resources_returns_contents():
    broker = MCPBroker()
    c1, _ = _connected_client("svc", contents=[{"uri": "file://a", "text": "hello"}])
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    contents = await broker.read_resource("svc", "file://a")
    assert contents == [{"uri": "file://a", "text": "hello"}]


@pytest.mark.asyncio
async def test_broker_read_unknown_server_raises():
    broker = MCPBroker()
    c1, _ = _connected_client("svc")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    with pytest.raises(MCPError) as exc_info:
        await broker.read_resource("nope", "file://a")
    assert 'Server "nope" not found' in str(exc_info.value)


@pytest.mark.asyncio
async def test_broker_read_not_connected_raises():
    broker = MCPBroker()
    c1, t1 = _connected_client("svc-down")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    await c1.disconnect()
    assert not c1.available
    with pytest.raises(MCPError) as exc_info:
        await broker.read_resource("svc-down", "file://a")
    assert "is not connected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_broker_read_missing_resources_capability_raises():
    broker = MCPBroker()
    c1, _ = _connected_client("svc", capabilities_resources=False)
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    with pytest.raises(MCPError) as exc_info:
        await broker.read_resource("svc", "file://a")
    assert "does not support resources" in str(exc_info.value)


# ---------- 层 3：静态工具契约 ----------


def _tools(broker: MCPBroker) -> tuple[Any, Any]:
    from bandi.tools.mcp_resources import McpListResourcesTool, McpReadResourceTool

    return McpListResourcesTool(broker), McpReadResourceTool(broker)


@pytest.mark.asyncio
async def test_tool_list_metadata_declares_read_only_and_no_dedup():
    broker = MCPBroker()
    lst, rd = _tools(broker)
    assert lst.category is ToolCategory.INTEGRATION
    assert lst.is_read_only and lst.is_concurrency_safe
    assert lst.dedup_scope == "none", "读外部状态禁止结果去重"
    assert lst.feature_group == "integration"
    assert "server" in (lst.parameters.get("properties") or {})
    assert rd.parameters.get("required") == ["server", "uri"]


@pytest.mark.asyncio
async def test_tool_list_empty_result_returns_message():
    broker = MCPBroker()
    c1, _ = _connected_client("svc")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    lst, _ = _tools(broker)
    result = await lst.execute({}, _ctx())
    assert result["success"] is True
    assert result["data"] == []
    assert "no resources" in str(result.get("message", "")).lower()


@pytest.mark.asyncio
async def test_tool_list_production_chain_reads_resources():
    broker = MCPBroker()
    c1, _ = _connected_client(
        "svc",
        resources=[
            {"uri": "file://a", "name": "A", "mimeType": "text/plain", "description": "doc"}
        ],
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    lst, _ = _tools(broker)
    result = await lst.execute({}, _ctx())
    assert result["success"] is True
    item = result["data"][0]
    assert item == {
        "uri": "file://a",
        "name": "A",
        "mimeType": "text/plain",
        "description": "doc",
        "server": "svc",
    }


@pytest.mark.asyncio
async def test_tool_list_unknown_server_returns_failure_not_raise():
    broker = MCPBroker()
    c1, _ = _connected_client("svc")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    lst, _ = _tools(broker)
    result = await lst.execute({"server": "nope"}, _ctx())
    assert result["success"] is False
    assert "nope" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_tool_read_text_content_passthrough():
    broker = MCPBroker()
    c1, _ = _connected_client(
        "svc", contents=[{"uri": "file://a", "mimeType": "text/plain", "text": "hi"}]
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    _, rd = _tools(broker)
    result = await rd.execute({"server": "svc", "uri": "file://a"}, _ctx())
    assert result["success"] is True
    entry = result["data"]["contents"][0]
    assert "blobSavedTo" not in entry
    assert entry["text"] == "hi"


@pytest.mark.asyncio
async def test_tool_read_blob_persisted_and_read_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    broker = MCPBroker()
    blob = b"\x89PNG\r\n\x1a\nbinary-payload"
    b64 = __import__("base64").b64encode(blob).decode()
    c1, _ = _connected_client(
        "svc", contents=[{"uri": "file://a", "mimeType": "image/png", "blob": b64}]
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    _, rd = _tools(broker)
    result = await rd.execute({"server": "svc", "uri": "file://a"}, _ctx())
    entry = result["data"]["contents"][0]
    saved = entry["blobSavedTo"]
    assert saved and str(tmp_path / "generated" / "mcp_resources") in str(Path(saved).parent)
    written = Path(saved).read_bytes()
    assert written == blob, "落盘字节必须与 blob 一致"
    assert str(Path(saved)).endswith(".png"), f"mime 派生扩展名应 .png: {saved}"


@pytest.mark.asyncio
async def test_tool_read_blob_unknown_mime_uses_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    broker = MCPBroker()
    blob = b"\x00\x01\x02\x03"
    b64 = __import__("base64").b64encode(blob).decode()
    c1, _ = _connected_client(
        "svc", contents=[{"uri": "mem://k", "mimeType": "application/x-vendor-weird", "blob": b64}]
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    _, rd = _tools(broker)
    result = await rd.execute({"server": "svc", "uri": "mem://k"}, _ctx())
    saved = result["data"]["contents"][0]["blobSavedTo"]
    Path(saved).read_bytes() == blob  # 断言落盘内容（最终产物）
    assert str(Path(saved)).endswith(".bin"), f"未知 mime 必须回落 .bin: {saved}"


@pytest.mark.asyncio
async def test_tool_read_blob_over_limit_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    broker = MCPBroker()
    blob = b"x" * 2048
    b64 = __import__("base64").b64encode(blob).decode()
    c1, _ = _connected_client(
        "svc", contents=[{"uri": "file://a", "mimeType": "image/png", "blob": b64}]
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.mcp_resources._MAX_BLOB_BYTES", 1024)
    _, rd = _tools(broker)
    result = await rd.execute({"server": "svc", "uri": "file://a"}, _ctx())
    entry = result["data"]["contents"][0]
    assert "blobSavedTo" not in entry, "超限 blob 不落盘"
    assert (
        "could not be saved" in entry.get("text", "").lower()
        or "limit" in entry.get("text", "").lower()
    )


@pytest.mark.asyncio
async def test_tool_read_blob_invalid_base64_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    broker = MCPBroker()
    c1, _ = _connected_client(
        "svc", contents=[{"uri": "file://a", "mimeType": "image/png", "blob": "!!!not-base64!!!"}]
    )
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    _, rd = _tools(broker)
    result = await rd.execute({"server": "svc", "uri": "file://a"}, _ctx())
    entry = result["data"]["contents"][0]
    assert "blobSavedTo" not in entry
    assert "could not be saved" in entry.get("text", "").lower()


@pytest.mark.asyncio
async def test_tool_read_missing_required_params_returns_failure():
    broker = MCPBroker()
    _, rd = _tools(broker)
    result = await rd.execute({}, _ctx())
    assert result["success"] is False
    assert "server" in str(result.get("error", "")) or "uri" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_tool_read_mcp_error_returns_failure_not_raise():
    broker = MCPBroker()
    c1, _ = _connected_client("svc")
    broker.register(c1, pool_name="pool-x")
    await broker.connect_all()
    _, rd = _tools(broker)
    result = await rd.execute({"server": "nope", "uri": "file://a"}, _ctx())
    assert result["success"] is False
    assert 'Server "nope" not found' in str(result.get("error", ""))


# ---------- 层 4：装配契约（生产主链路） ----------


@pytest.mark.asyncio
async def test_assembly_build_mcp_broker_registers_static_tools():
    """E2E 生产装配：config → build_mcp_broker → registry 含两静态工具 → execute 最终产物。
    仅 mock 传输层，MCPClient/MCPBroker/装配全真实。"""
    from unittest.mock import patch

    from bandi.agent.factory import build_mcp_broker
    from bandi.core.config import BandiConfig, MCPConfig

    transport = _MockTransport()
    transport.queue(
        "initialize",
        {"serverInfo": {"name": "svc"}, "capabilities": {"resources": {"subscribe": False}}},
    )
    transport.queue("tools/list", {"tools": []})
    transport.queue("resources/list", {"resources": [{"uri": "file://a", "name": "A"}]})

    cfg = BandiConfig()
    cfg.tools.mcp_servers = [MCPConfig(name="svc", transport="stdio", command="echo", args=[])]
    registry = ToolRegistry()
    with patch("bandi.tools.mcp_client.StdioTransport", return_value=transport):
        broker = await build_mcp_broker(cfg, registry=registry)
    assert broker is not None
    try:
        lst = registry.get("mcp_list_resources")
        rd = registry.get("mcp_read_resource")
        assert lst is not None and rd is not None, "装配必须注册两个静态 MCP 工具"
        result = await lst.execute({}, _ctx())
        assert result["success"] is True
        assert result["data"][0]["server"] == "svc"
        assert result["data"][0]["uri"] == "file://a"
    finally:
        await broker.disconnect_all()


@pytest.mark.asyncio
async def test_assembly_no_servers_registers_nothing():
    from unittest.mock import patch

    from bandi.agent.factory import build_mcp_broker
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.tools.mcp_servers = []
    registry = ToolRegistry()
    with patch("bandi.tools.mcp_client.StdioTransport"):
        broker = await build_mcp_broker(cfg, registry=registry)
    assert broker is None
    assert registry.get("mcp_list_resources") is None
    assert registry.get("mcp_read_resource") is None


def test_assembly_feature_gate_off_disables_tools():
    from bandi.tools.mcp_resources import (
        register_mcp_resource_tools,
    )

    registry = ToolRegistry(feature_flags={"integration": False})
    broker = MCPBroker()
    register_mcp_resource_tools(registry, broker)
    lst = registry.get("mcp_list_resources")
    rd = registry.get("mcp_read_resource")
    assert lst is None, "integration gate 关闭时工具必须 fail-closed"
    assert rd is None
