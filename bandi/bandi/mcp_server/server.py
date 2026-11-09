# 功能：MCP Server（10.8）——JSON-RPC 2.0 stdio 服务端，暴露 knowledge/skills/memory
# 对齐 tools/mcp_client.py 手写协议风格（仓内无 mcp 依赖）；
# 协议：initialize / tools/list / tools/call / ping（notification 无 id 不回包）。
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bandi"
SERVER_VERSION = "0.1.0"

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]
ToolSpec = tuple[ToolFn, dict[str, Any], str]


def _knowledge_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    from bandi.knowledge.store import get_shared_store

    hits = get_shared_store().search_chunks(query, limit=limit)
    return {
        "query": query,
        "total": len(hits),
        "results": [
            {
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "bucket_id": chunk.bucket_id,
                "source_ref": chunk.source_ref,
                "content": chunk.content[:800],
            }
            for chunk in hits
        ],
    }


def _skills_list(args: dict[str, Any]) -> dict[str, Any]:
    from bandi.skills.manager import create_skill_registry

    skills = create_skill_registry().list_all()
    return {
        "total": len(skills),
        "skills": [
            {"name": s.name, "description": s.description, "tags": list(s.tags or [])}
            for s in skills
        ],
    }


def _default_long_term_path() -> Path:
    """功能：主链长期记忆库路径（与 agent_runner/factory 同源）——禁止裸构造
    `LongTermMemory()`（默认 `:memory:` 会静默读空库）。"""
    return Path.home() / ".bandi" / "data" / "long_term.db"


def _memory_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    from bandi.memory.long_term import LongTermMemory

    db_path = _default_long_term_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    memory = LongTermMemory(str(db_path))
    try:
        hits = memory.search_fts(query, limit=limit)
        return {
            "query": query,
            "total": len(hits),
            "results": [
                {"item_id": hit.id, "content": hit.content[:800], "importance": hit.importance}
                for hit in hits
            ],
        }
    finally:
        memory.close()


def default_tools() -> dict[str, ToolSpec]:
    """功能：默认工具面（知识检索/技能列表/长期记忆检索）——name → (fn, schema, desc)。

    本体查询不在此暴露：Agent 面已有原生 `ontology_query`（六模式同源），
    自桥接的 MCP 重复面只会制造同名工具噪音。"""
    query_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    }
    return {
        "bandi_knowledge_search": (
            _knowledge_search,
            query_schema,
            "检索 Bandi 知识库（三层 chunk 命中）",
        ),
        "bandi_skills_list": (
            _skills_list,
            {"type": "object", "properties": {}},
            "列出 Bandi 已安装技能（名/描述/标签）",
        ),
        "bandi_memory_search": (
            _memory_search,
            query_schema,
            "检索 Bandi 长期记忆（FTS）",
        ),
    }


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(
    request: dict[str, Any],
    *,
    tools: dict[str, ToolSpec] | None = None,
) -> dict[str, Any] | None:
    """功能：单条 JSON-RPC 请求处理——notification（无 id）返回 None（不回包）。"""
    if not isinstance(request, dict):
        return _error_response(None, -32600, "invalid request")
    request_id = request.get("id")
    method = str(request.get("method") or "")
    tool_map = tools if tools is not None else default_tools()

    if method == "initialize":
        result: Any = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, "description": desc, "inputSchema": schema}
                for name, (_, schema, desc) in tool_map.items()
            ]
        }
    elif method == "tools/call":
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        spec = tool_map.get(name)
        if spec is None:
            return _error_response(request_id, -32602, f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error_response(request_id, -32602, "arguments must be an object")
        try:
            payload = spec[0](arguments)
        except Exception as e:  # noqa: BLE001 — 工具异常按 MCP isError 回包，不炸服务
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            result = {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": False,
            }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    else:
        if request_id is None:
            return None
        return _error_response(request_id, -32601, f"method not found: {method}")

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _ensure_utf8(stream: TextIO) -> TextIO:
    """功能：协议流强制 UTF-8——Windows 控制台默认 gbk 会让中文/emoji 乱码或
    编码异常（MCP stdio 规定 UTF-8 字节流）；StringIO 等无 reconfigure 的
    测试流原样返回。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    return stream


def serve_stdio_sync(stdin: TextIO, stdout: TextIO) -> None:
    """功能：stdio 主循环——按行读 JSON-RPC，逐条回包并 flush（外部宿主进程协议）。"""
    stdin = _ensure_utf8(stdin)
    stdout = _ensure_utf8(stdout)
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            response = _error_response(None, -32700, "parse error")
        else:
            response = handle_request(request)
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()


def main() -> None:  # pragma: no cover — CLI 入口薄壳
    serve_stdio_sync(sys.stdin, sys.stdout)


__all__ = [
    "PROTOCOL_VERSION",
    "default_tools",
    "handle_request",
    "serve_stdio_sync",
]
