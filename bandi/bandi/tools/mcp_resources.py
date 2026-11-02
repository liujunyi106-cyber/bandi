# 功能：MCP 静态资源工具族（mcp_list_resources / mcp_read_resource + 装配注册）
from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import workspace_root
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import MCPError
from bandi.tools.registry import ToolRegistry

_MAX_BLOB_BYTES: int = 10 * 1024 * 1024

# MIME→扩展名白名单：未知类型回落 .bin（禁止用未白名单扩展名拼路径）
_MIME_EXT: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "font/ttf": ".ttf",
    "application/octet-stream": ".bin",
}
_DEFAULT_EXT: str = ".bin"


def _ext_for_mime(mime: str | None) -> str:
    if not mime:
        return _DEFAULT_EXT
    return _MIME_EXT.get(mime.strip().split(";")[0].strip().lower(), _DEFAULT_EXT)


def _persist_blob(payload: str, mime: str | None, index: int) -> str | None:
    # 上限预判：base64 长度超限直接拒绝，避免解码超大字符串
    if len(payload) > (_MAX_BLOB_BYTES * 4 // 3) + 4:
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > _MAX_BLOB_BYTES:
        return None
    ext = _ext_for_mime(mime)
    token = hashlib.sha256(raw).hexdigest()[:12]
    target = workspace_root() / "generated" / "mcp_resources" / f"mcp-resource-{index}-{token}{ext}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError:
        return None
    return str(target)


class McpListResourcesTool(BaseTool):
    name = "mcp_list_resources"
    description = (
        "List resources exposed by connected MCP servers. "
        'Optional "server" filters by server name; missing value lists all. '
        "Each row shows uri/name/mimeType/description/server. Return value may be empty "
        "when a server provides no resources even though it still offers tools."
    )
    category = ToolCategory.INTEGRATION
    feature_group = "integration"
    risk_level = "low"
    is_read_only = True
    is_concurrency_safe = True
    # 读外部状态（服务器资源每次列都变化），结果禁去重
    dedup_scope = "none"
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "可选：按 MCP 服务器名过滤资源列表",
            },
        },
    }

    def __init__(self, broker: MCPBroker):
        super().__init__()
        self._broker = broker

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str | dict[str, Any]:
        try:
            server = params.get("server") if isinstance(params, dict) else None
            resources = await self._broker.list_resources(server=server)
        except MCPError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "data": resources,
            "message": (
                "No resources found. MCP servers may still provide tools "
                "even if they have no resources."
            )
            if not resources
            else None,
        }


class McpReadResourceTool(BaseTool):
    # 安全（③⁻）：MCP 远端资源内容不可信——输出按不可信外部内容包裹（executor 6.14）
    external_content = True
    name = "mcp_read_resource"
    description = (
        "Read a resource from a connected MCP server by server name and uri. "
        "Text content returned inline; binary (blob) content is base64-decoded, written "
        "to the workspace generated/mcp_resources directory, and the saved path returned "
        "in blobSavedTo with a text confirmation. Failures (server not found / not "
        "connected / no resources capability / blob decode / size limit / write error) "
        "are reported as text, never crash the run."
    )
    category = ToolCategory.INTEGRATION
    feature_group = "integration"
    risk_level = "low"
    is_read_only = True
    is_concurrency_safe = True
    dedup_scope = "none"
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "MCP 服务器名（与配置 mcp_servers 中的 name 一致）",
            },
            "uri": {
                "type": "string",
                "description": "要读取的资源 URI",
            },
        },
        "required": ["server", "uri"],
    }

    def __init__(self, broker: MCPBroker):
        super().__init__()
        self._broker = broker

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str | dict[str, Any]:
        if not isinstance(params, dict):
            return {"success": False, "error": "params must be an object"}
        missing = [k for k in ("server", "uri") if not params.get(k)]
        if missing:
            return {"success": False, "error": f"missing required param: {', '.join(missing)}"}
        try:
            contents = await self._broker.read_resource(params["server"], params["uri"])
        except MCPError as exc:
            return {"success": False, "error": str(exc)}
        processed: list[dict[str, Any]] = []
        for idx, item in enumerate(contents):
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {"uri": item.get("uri", params["uri"])}
            if item.get("mimeType"):
                entry["mimeType"] = item["mimeType"]
            text = item.get("text")
            blob = item.get("blob")
            if isinstance(text, str):
                entry["text"] = text
            elif isinstance(blob, str):
                saved = _persist_blob(blob, item.get("mimeType"), idx)
                if saved is not None:
                    entry["blobSavedTo"] = saved
                    entry["text"] = f"Binary content saved to {saved}"
                else:
                    entry["text"] = (
                        "Binary content could not be saved to disk "
                        "(decode error, size exceeded limit, or write failure)"
                    )
            processed.append(entry)
        return {"success": True, "data": {"contents": processed}}


def register_mcp_resource_tools(registry: ToolRegistry, broker: MCPBroker) -> None:
    # 功能：装配静态 MCP 资源工具（与 build_mcp_broker 动态工具注册并列）
    registry.register_instance(McpListResourcesTool(broker))
    registry.register_instance(McpReadResourceTool(broker))
