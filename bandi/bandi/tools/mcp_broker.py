# 功能：MCP 中间件代理（工具注册桥接 MCP 服务）
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from bandi.tools.mcp_client import MCPClient, MCPError, register_mcp_tools
from bandi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MCPBroker:
    def __init__(self, health_check_interval: float = 30.0):
        self._pools: dict[str, list[MCPClient]] = {}
        self._prefixes: dict[int, str | None] = {}
        self._allowed_tools: dict[int, set[str] | None] = {}
        self._rr_indices: dict[str, int] = {}
        self._registry: ToolRegistry | None = None
        self._client_prefixes: dict[int, str] = {}
        self._health_check_interval = health_check_interval
        self._health_task: asyncio.Task | None = None
        self._running = False

    def register(
        self,
        client: MCPClient,
        pool_name: str | None = None,
        prefix: str | None = None,
        allowed_tools: set[str] | None = None,
    ) -> None:
        name = pool_name or client.name
        if name not in self._pools:
            self._pools[name] = []
            self._rr_indices[name] = 0
        # prefix 按 client 存储
        self._prefixes[id(client)] = prefix
        self._allowed_tools[id(client)] = allowed_tools
        self._pools[name].append(client)
        logger.info(
            "MCP client %s registered to pool %s (total: %d)",
            client.name,
            name,
            len(self._pools[name]),
        )

    def unregister(self, client: MCPClient) -> None:
        for name, clients in list(self._pools.items()):
            if client in clients:
                clients.remove(client)
                self._prefixes.pop(id(client), None)
                self._allowed_tools.pop(id(client), None)
                logger.info("MCP client %s removed from pool %s", client.name, name)
            if not clients:
                del self._pools[name]
                self._rr_indices.pop(name, None)

    async def connect_all(self) -> dict[str, list[Exception]]:
        errors: dict[str, list[Exception]] = {}
        for name, clients in self._pools.items():
            results = await asyncio.gather(
                *(self._connect_one(c) for c in clients), return_exceptions=True
            )
            pool_errors = [r for r in results if isinstance(r, Exception)]
            if pool_errors:
                errors[name] = pool_errors
        return errors

    async def _connect_one(self, client: MCPClient) -> None:
        await client.connect()

    async def disconnect_all(self) -> None:
        # （stale 引用，disconnect 后 client 又被 reconnect 复活）
        await self.stop_health_check()
        for clients in self._pools.values():
            await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)

    def get_client(self, pool_name: str) -> MCPClient | None:
        clients = self._pools.get(pool_name)
        if not clients:
            return None
        available = [c for c in clients if c.available]
        if not available:
            return None
        idx = self._rr_indices.get(pool_name, 0) % len(available)
        self._rr_indices[pool_name] = idx + 1
        return available[idx]

    async def call_tool(
        self, pool_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        client = self.get_client(pool_name)
        if not client:
            raise MCPError(f"no available client in pool: {pool_name}")
        return await client.call_tool(tool_name, arguments)

    def _all_clients(self) -> list[MCPClient]:
        clients: list[MCPClient] = []
        for pool_clients in self._pools.values():
            clients.extend(pool_clients)
        return clients

    def _server_not_found_error(self, server: str) -> MCPError:
        available = ", ".join(c.name for c in self._all_clients()) or "none"
        return MCPError(f'Server "{server}" not found. Available servers: {available}')

    async def list_resources(self, server: str | None = None) -> list[dict[str, Any]]:
        clients = self._all_clients()
        if server is not None:
            matched = [c for c in clients if c.name == server]
            if not matched:
                raise self._server_not_found_error(server)
            candidates = matched
        else:
            candidates = clients
        resources: list[dict[str, Any]] = []
        for client in candidates:
            if not client.available:
                continue
            try:
                items = await client.list_resources()
            except MCPError:
                # server 过滤下单 client 失败不影响整体列资源
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                resources.append(
                    {
                        "uri": str(item.get("uri", "")),
                        "name": str(item.get("name") or item.get("uri") or ""),
                        "mimeType": item.get("mimeType"),
                        "description": item.get("description"),
                        "server": client.name,
                    }
                )
        return resources

    async def read_resource(self, server: str, uri: str) -> list[dict[str, Any]]:
        clients = self._all_clients()
        client = next((c for c in clients if c.name == server), None)
        if client is None:
            raise self._server_not_found_error(server)
        if not client.available:
            raise MCPError(f'Server "{server}" is not connected')
        if "resources" not in client.capabilities:
            raise MCPError(f'Server "{server}" does not support resources')
        contents = await client.read_resource(uri)
        return contents if isinstance(contents, list) else []

    async def register_all_tools(
        self, registry: ToolRegistry, prefix: str | None = None
    ) -> dict[str, list[str]]:
        results: dict[str, list[str]] = {}
        self._registry = registry
        for name, clients in self._pools.items():
            for index, client in enumerate(clients):
                if client.available:
                    client_prefix = prefix or self._prefixes.get(id(client)) or name
                    if prefix is None and len(clients) > 1:
                        # 功能：同 pool 多 client 必须使用独立 namespace，否则后注册者
                        # 的 unregister_prefix 会把前一个 client 的工具整批清掉。
                        safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", client.name).strip("_")
                        client_prefix = f"{client_prefix}__{safe_name or f'c{index}'}"
                    self._client_prefixes[id(client)] = client_prefix
                    tools = await register_mcp_tools(
                        registry,
                        client,
                        prefix=client_prefix,
                        allowed_tools=self._allowed_tools.get(id(client)),
                    )
                    results.setdefault(name, []).extend(tools)
        return results

    def start_health_check(self) -> None:
        if self._running:
            return
        self._running = True
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info("MCP health check started (interval: %.1fs)", self._health_check_interval)

    async def stop_health_check(self) -> None:
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — 已结束任务的异常不得阻断 disconnect_all
                logger.debug("MCP health task ended with error: %s", exc)
            self._health_task = None
        logger.info("MCP health check stopped")

    async def _health_check_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._health_check_interval)
            for name, clients in list(self._pools.items()):
                for client in clients:
                    if not client.available:
                        logger.warning(
                            "Health check: MCP client %s in pool %s unavailable, reconnecting",
                            client.name,
                            name,
                        )
                        try:
                            ok = await client.reconnect()
                        except Exception as exc:  # noqa: BLE001 — 单个 client 失败不得杀死健康循环
                            logger.warning(
                                "Health check: MCP client %s reconnect raised: %s",
                                client.name,
                                exc,
                            )
                            ok = False
                        if ok:
                            logger.info(
                                "Health check: MCP client %s in pool %s reconnected",
                                client.name,
                                name,
                            )
                            if self._registry is not None:
                                try:
                                    await register_mcp_tools(
                                        self._registry,
                                        client,
                                        prefix=self._client_prefixes.get(id(client), name),
                                        allowed_tools=self._allowed_tools.get(id(client)),
                                    )
                                except Exception as exc:  # noqa: BLE001 — 重注册失败下次循环再试
                                    logger.warning(
                                        "Health check: re-register tools failed for %s: %s",
                                        client.name,
                                        exc,
                                    )
                        else:
                            logger.error(
                                "Health check: MCP client %s in pool %s reconnect failed",
                                client.name,
                                name,
                            )

    @property
    def pool_names(self) -> list[str]:
        return list(self._pools.keys())

    @property
    def total_clients(self) -> int:
        return sum(len(clients) for clients in self._pools.values())

    def get_pool_summary(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "total": len(clients),
                "available": sum(1 for c in clients if c.available),
                "clients": [c.name for c in clients],
            }
            for name, clients in self._pools.items()
        }
