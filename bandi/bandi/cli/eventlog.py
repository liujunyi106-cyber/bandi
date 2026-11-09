# 功能：EventLog 审计命令——只读重放校验事件日志的完整性（hash 链/sequence/event_id）
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

eventlog_app = typer.Typer(help="事件日志审计")

console = Console()


@eventlog_app.command("audit")
def eventlog_audit(
    path: str = typer.Argument(help="事件 JSONL 文件路径"),
) -> None:
    """功能：只读重放校验事件日志——hash 链完整性 / sequence 单调 / event_id 唯一 / 损坏行定位。

    纯读审计：不改写文件、不旋转、不重置状态。校验结果打印完整性报告。
    """
    from bandi.core.event_bus import EventBus

    log_path = Path(path)
    if not log_path.exists():
        console.print(f"[red]事件日志不存在: {log_path}[/red]")
        raise typer.Exit(1)
    report = EventBus(persist_path=log_path).replay(dry_run=True)
    console.print(f"[bold]事件日志审计[/bold] {log_path}")
    console.print(f"  总行数: {report.lines_read} / 有效行: {report.valid_lines}")
    console.print(f"  sequence: [{report.first_sequence} .. {report.last_sequence}]")
    if report.hash_chain_broken:
        console.print("[red]  !! hash 链断裂（日志被篡改或写中断）[/red]")
    else:
        console.print("[green]  哈希链完整[/green]")
    if report.duplicate_event_ids:
        console.print(f"[red]  重复 event_id: {report.duplicate_event_ids}[/red]")
    for err in report.errors:
        console.print(f"[yellow]  L{err.line_no} {err.kind}: {err.detail}[/yellow]")
    if report.ok:
        console.print("[green]校验通过[/green]")
    else:
        console.print("[red]校验未通过（见上方错误）[/red]")
        raise typer.Exit(1)
