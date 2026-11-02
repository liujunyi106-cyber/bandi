# 功能：MCP CLI（10.8）——启动 stdio MCP Server 供外部宿主接入
from __future__ import annotations

import sys

import typer

mcp_app = typer.Typer(help="MCP server 管理")


@mcp_app.command("serve")
def serve_cmd() -> None:
    """stdio MCP server：暴露知识检索/技能列表/记忆检索给外部宿主。"""
    from bandi.mcp_server.server import serve_stdio_sync

    serve_stdio_sync(sys.stdin, sys.stdout)
