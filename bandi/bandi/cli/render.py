# 功能：rich 渲染工具 — explain 轨迹 / audit 报表

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.tree import Tree

from bandi.memory.session.store import SessionStore


def render_explain(
    session_id: str,
    store: SessionStore,
    console: Console | None = None,
    at: str | None = None,
) -> None:
    console = console or Console()
    try:
        entries = store.resolved_branch(session_id, at=at)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    tree = Tree(f"执行轨迹: {session_id}")
    for entry in entries:
        if entry.get("entry_type") == "message" and entry.get("role") == "assistant":
            branch = tree.add(f"[bold]第 {entry.get('turn_index', '?')} 轮[/bold]")
            content = (entry.get("content") or "")[:200]
            branch.add(f"内容: {content}")
            meta = entry.get("metadata", "{}")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            tool_calls = meta.get("tool_calls", [])
            if tool_calls:
                tools_branch = branch.add("工具调用:")
                for tc in tool_calls:
                    tools_branch.add(f"{tc.get('name', '?')}({tc.get('arguments', {})})")
    console.print(tree)
