# 功能：MCP Server 包（10.8）——把 Bandi 能力（知识/技能/记忆）暴露给外部宿主
from bandi.mcp_server.server import handle_request, serve_stdio_sync

__all__ = ["handle_request", "serve_stdio_sync"]
