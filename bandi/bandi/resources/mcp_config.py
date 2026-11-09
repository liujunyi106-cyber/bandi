# 功能：MCP 配置文档（~/.bandi/mcp.json）与 MCPConfig 互转、校验、保存。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.core.mcp_file import (
    MCPFileError,
    mcp_config_path,
    read_servers_raw,
    servers_from_document,
    write_servers_raw,
)


class MCPConfigError(Exception):
    """MCP 配置无法读取或保存。"""


def load_servers(*, strict: bool = False) -> list[Any]:
    """功能：读取生效的 MCP 服务列表（mcp.json 优先，缺失回退 config.yaml）。"""
    target = mcp_config_path()
    if target.is_file():
        try:
            raw = read_servers_raw(target) or []
            return [_to_config(item) for item in raw]
        except (MCPFileError, ValueError) as exc:
            if strict:
                raise MCPConfigError(str(exc)) from exc
    from bandi.core.config import BandiConfig

    return list(BandiConfig.load().tools.mcp_servers)


def save_servers(servers: list[Any]) -> Path:
    """功能：把 MCP 服务列表写入 mcp.json。"""
    raw = [_to_raw(server) for server in servers]
    try:
        return write_servers_raw(raw)
    except MCPFileError as exc:
        raise MCPConfigError(str(exc)) from exc


def load_document() -> dict[str, Any]:
    """功能：读取 mcp.json 的编辑文档；文件缺失时由生效配置生成。"""
    target = mcp_config_path()
    if target.is_file():
        try:
            raw = read_servers_raw(target) or []
        except (MCPFileError, ValueError) as exc:
            raise MCPConfigError(str(exc)) from exc
    else:
        raw = [_to_raw(server) for server in load_servers()]
    return {"mcpServers": {item["name"]: _public_entry(item) for item in raw}}


def save_document(document: Any) -> Path:
    """功能：校验并保存 mcpServers 文档。"""
    try:
        raw = servers_from_document(document)
    except (MCPFileError, ValueError) as exc:
        raise MCPConfigError(str(exc)) from exc
    for item in raw:
        _to_config(item)
    try:
        return write_servers_raw(raw)
    except MCPFileError as exc:
        raise MCPConfigError(str(exc)) from exc


def _to_config(item: dict[str, Any]) -> Any:
    """功能：原始字典 → MCPConfig（字段校验由此统一完成）。"""
    from bandi.core.config import MCPConfig

    try:
        return MCPConfig.model_validate(item)
    except ValueError as exc:
        raise MCPConfigError(f"MCP 服务 {item.get('name') or '?'} 配置不合法: {exc}") from exc


def _to_raw(server: Any) -> dict[str, Any]:
    """功能：MCPConfig → mcp.json 原始字典。"""
    return {
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": dict(server.env),
        "url": server.url,
        "prefix": server.prefix,
        "allowed_tools": server.allowed_tools,
        "enabled": server.enabled,
        "category": server.category,
        "homepage": server.homepage,
    }


def _public_entry(item: dict[str, Any]) -> dict[str, Any]:
    """功能：单个服务在 mcp.json 中的展示形态（省略默认值）。"""
    transport = str(item.get("transport") or "stdio")
    entry: dict[str, Any] = {}
    if transport != "stdio":
        entry["transport"] = transport
    if transport == "stdio":
        entry["command"] = str(item.get("command") or "")
        if item.get("args"):
            entry["args"] = list(item.get("args") or [])
    else:
        entry["url"] = str(item.get("url") or "")
    if item.get("env"):
        entry["env"] = dict(item.get("env") or {})
    if item.get("prefix"):
        entry["prefix"] = str(item["prefix"])
    if item.get("allowed_tools") is not None:
        entry["allowed_tools"] = list(item.get("allowed_tools") or [])
    if item.get("enabled") is False:
        entry["enabled"] = False
    if item.get("category"):
        entry["category"] = str(item["category"])
    if item.get("homepage"):
        entry["homepage"] = str(item["homepage"])
    return entry
