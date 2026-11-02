from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

memory_app = typer.Typer(help="记忆管理")


@memory_app.callback()
def _memory_root() -> None:
    """记忆管理命令组（保持 bandi memory <command> 二级形态）。"""


def _default_db() -> Path:
    # 延迟求值：import 时固化 Path.home() 会破坏 monkeypatch/便携部署
    return Path.home() / ".bandi" / "data" / "hierarchy_global.db"


_MAX_LIMIT = 500


def query_memory_events(
    db_path: str | Path,
    event_type: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    limit 钳制 [1, 500] 防全表拖取；库文件不存在视为零事件（不静默建库）。"""
    path = Path(db_path)
    if not path.exists():
        return []
    from bandi.memory.global_store import GlobalMemoryStore

    capped = max(1, min(int(limit), _MAX_LIMIT))
    store = GlobalMemoryStore(str(path))
    try:
        if event_type:
            return store.query_events(event_type=event_type, limit=capped)
        return store.list_events(limit=capped)
    finally:
        store.close()


@memory_app.command("events")
def events_cmd(
    event_type: str = typer.Option("", "--type", "-t", help="按事件类型过滤（如 memory_persist）"),
    limit: int = typer.Option(50, "--limit", "-l", help="返回条数上限"),
    db: str = typer.Option("", "--db", help="hierarchy_global.db 路径（默认 ~/.bandi/data）"),
) -> None:
    """查询全局事件溯源表（审计/复盘读方）。"""
    target = Path(db) if db else _default_db()
    rows = query_memory_events(target, event_type=event_type, limit=limit)
    typer.echo(json.dumps(rows, ensure_ascii=False, default=str))
