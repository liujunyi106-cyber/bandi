# 功能：mcp.json 低层读写（不依赖 BandiConfig，供配置加载与资源中心共用）。
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[^\s/\\\x00-\x1f]{1,64}$")


class MCPFileError(Exception):
    """mcp.json 不存在以外的读取/结构错误。"""


def mcp_config_path() -> Path:
    """功能：mcp.json 路径（默认 ~/.bandi/mcp.json）。"""
    override = os.environ.get("BANDI_MCP_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bandi" / "mcp.json"


def _normalize_transport(value: str, item: dict[str, Any]) -> str:
    transport = str(value or "").strip().lower()
    if transport in {"http", "sse", "streamable_http", "streamable-http"}:
        return "streamable_http"
    if transport in {"stdio", "command"}:
        return "stdio"
    return "streamable_http" if item.get("url") else "stdio"


def servers_from_document(document: Any) -> list[dict[str, Any]]:
    """功能：把 mcpServers 文档转成 MCPConfig 原始字典列表。"""
    if not isinstance(document, dict):
        raise MCPFileError("mcp.json 顶层必须是对象")
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raise MCPFileError("mcp.json 必须包含 mcpServers 对象")
    servers: list[dict[str, Any]] = []
    for name, raw in raw_servers.items():
        key = str(name or "").strip()
        if not _NAME_RE.fullmatch(key):
            raise MCPFileError(f"非法 MCP 服务名: {name!r}")
        if not isinstance(raw, dict):
            raise MCPFileError(f"MCP 服务 {key} 的配置必须是对象")
        item = {k: v for k, v in raw.items() if k != "type"}
        item["name"] = key
        item["transport"] = _normalize_transport(
            str(raw.get("transport") or raw.get("type") or ""), raw
        )
        if item["transport"] == "stdio":
            if not str(item.get("command") or "").strip():
                raise MCPFileError(f"MCP 服务 {key} 缺少 command")
        elif not str(item.get("url") or "").strip():
            raise MCPFileError(f"MCP 服务 {key} 缺少 url")
        servers.append(item)
    return servers


def read_servers_raw(path: Path | None = None) -> list[dict[str, Any]] | None:
    """功能：读取 mcp.json 的服务列表；文件不存在返回 None。"""
    target = path or mcp_config_path()
    if not target.is_file():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPFileError(f"读取 mcp.json 失败: {exc}") from exc
    return servers_from_document(document)


def write_servers_raw(servers: list[dict[str, Any]], path: Path | None = None) -> Path:
    """功能：把服务列表写成 mcp.json（原子替换）。"""
    target = path or mcp_config_path()
    document = {"mcpServers": {str(item["name"]): _public_entry(item) for item in servers}}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MCPFileError(f"写入 mcp.json 失败: {exc}") from exc
    return target


def _public_entry(item: dict[str, Any]) -> dict[str, Any]:
    """功能：单个服务配置的 mcp.json 出参（省略默认值）。"""
    created: dict[str, Any] = {}
    transport = str(item.get("transport") or "stdio")
    if transport != "stdio":
        created["transport"] = transport
    if transport == "stdio":
        created["command"] = str(item.get("command") or "")
        if item.get("args"):
            created["args"] = list(item.get("args") or [])
    else:
        created["url"] = str(item.get("url") or "")
    if item.get("env"):
        created["env"] = dict(item.get("env") or {})
    if item.get("prefix"):
        created["prefix"] = str(item["prefix"])
    if item.get("allowed_tools") is not None:
        created["allowed_tools"] = list(item.get("allowed_tools") or [])
    if item.get("enabled") is False:
        created["enabled"] = False
    if item.get("category"):
        created["category"] = str(item["category"])
    if item.get("homepage"):
        created["homepage"] = str(item["homepage"])
    return created
