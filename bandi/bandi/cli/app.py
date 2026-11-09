# 功能：Bandi CLI — typer 命令路由（chat / init / session / explain / entity / eval / serve）

from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bandi.eval.judge import AgentOutput
from bandi.utils.db import connect

if TYPE_CHECKING:
    from bandi.llm.base import BaseProvider
    from bandi.memory.session.store import SessionStore


import typer
from rich.console import Console

from bandi.cli.agents import agents_app
from bandi.cli.chat import ChatLoop
from bandi.cli.collab import collab_app
from bandi.cli.company import company_app
from bandi.cli.eventlog import eventlog_app
from bandi.cli.mcp import mcp_app
from bandi.cli.memory import memory_app
from bandi.cli.skills import skills_app
from bandi.cli.team import team_app


@dataclass
class _BenchmarkAgentOutput:
    final_message: str
    execution_trace: str
    output: str = ""


console = Console()
app = typer.Typer(name="bandi", help="Bandi — 个人多 Agent 数字员工组织（组织向）")
session_app = typer.Typer(help="会话管理")
entity_app = typer.Typer(help="Ontology 实体管理")
app.add_typer(session_app, name="session")
app.add_typer(entity_app, name="entity")
app.add_typer(company_app, name="company")
app.add_typer(collab_app, name="collab")
app.add_typer(memory_app, name="memory")
app.add_typer(eventlog_app, name="eventlog")
app.add_typer(skills_app, name="skills")
app.add_typer(team_app, name="team")
app.add_typer(agents_app, name="agents")
app.add_typer(mcp_app, name="mcp")
badcase_app = typer.Typer(help="Badcase 管理")
app.add_typer(badcase_app, name="badcase")


def _trigger_models_dev_sync() -> None:
    """功能：CLI 启动时按配置触发 models.dev 元数据同步（委托共享实现，与 API 启动同源）。"""
    from bandi.llm.models_dev import trigger_models_dev_sync

    result = trigger_models_dev_sync()
    if result and result.updated:
        console.print(
            f"[dim]models.dev 同步: {result.source}，更新 {len(result.updated)} 个模型[/dim]"
        )


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    project: str = typer.Option(
        "", "--project", help="层级记忆项目作用域（默认不限定，透传 run_agent_turn project_id）"
    ),
    employee: str = typer.Option(
        "", "--employee", help="层级记忆员工作用域（默认不限定，透传 run_agent_turn employee_id）"
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        _trigger_models_dev_sync()
        loop = ChatLoop(
            console=console,
            project_id=project or None,
            employee_id=employee or None,
        )
        asyncio.run(loop.run())


@app.command()
def init() -> None:
    config_dir = Path.home() / ".bandi"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "workspace").mkdir(exist_ok=True)
    (config_dir / "data").mkdir(exist_ok=True)

    console.print("[bold]Bandi 初始化[/bold]")

    model = typer.prompt("默认模型", default="deepseek-v4.1-flash")
    console.print("[dim]TIP: 也可设置 BANDI_API_KEY 环境变量（优先级高于配置文件）[/dim]")
    api_key = typer.prompt("API Key（留空使用 Mock 模式）", default="", hide_input=False)

    from bandi.core.config import BandiConfig

    config = BandiConfig(llm={"default_model": model, "api_key": api_key})

    config_path = BandiConfig._bandi_home() / "config.yaml"
    config.save(str(config_path))
    try:
        os.chmod(str(config_path), 0o600)
    except OSError:
        pass

    console.print(f"[green]配置已保存至 {config_path}[/green]")
    if not api_key:
        console.print("[yellow]Mock 模式：对话将返回模拟回复（配置 API Key 后重启即可）[/yellow]")
    console.print("[green]输入 bandi 开始对话[/green]")


@app.command()
def explain(
    session_id: str = typer.Argument(help="会话 ID"),
    at: str = typer.Option("", "--at", "-a", help="预览恢复点 entry_id（含自身截断，不删数据）"),
) -> None:
    from bandi.cli.render import render_explain
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在，请先运行 bandi init[/red]")
        raise typer.Exit(1)
    store = SessionStore(str(db_path))
    render_explain(session_id, store, console, at=at or None)


@app.command()
def audit(
    days: int = typer.Option(7, "--days", "-d", help="审计天数"),
    format: str = typer.Option("text", "--format", "-f", help="输出格式: text/json"),
) -> None:
    """生成审计报表"""
    from datetime import UTC, datetime, timedelta

    from bandi.memory.session.store import SessionStore
    from bandi.observability.audit_reporter import AuditFilter, AuditReporter

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在[/red]")
        raise typer.Exit(1)
    store = SessionStore(str(db_path))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    filter_obj = AuditFilter(time_from=cutoff)
    reporter = AuditReporter(store)
    report = reporter.generate(filter_obj)
    if format == "json":
        console.print(reporter.export_json(filter_obj))
    else:
        from rich.panel import Panel

        panel = Panel(
            f"[bold]审计报表[/bold] (过去 {days} 天)\n\n"
            f"会话数: {report.summary['total_sessions']}\n"
            f"消息轮次: {report.summary['total_turns']}\n"
            f"工具调用: {report.summary['total_tool_calls']}",
        )
        console.print(panel)
    reporter.record_event(
        {"action": "audit_report", "days": days, "format": format, **report.summary}
    )


@app.command()
def audit_verify() -> None:
    """验证审计哈希链完整性（链异常/被篡改时退出码 1，供脚本与 CI 判定）"""
    from bandi.memory.session.store import SessionStore
    from bandi.observability.audit_reporter import AuditReporter

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[yellow]数据库不存在，链文件可能为空[/yellow]")
    store = SessionStore(str(db_path))
    reporter = AuditReporter(store)
    issues = reporter.verify()
    if not issues:
        console.print("[green]审计哈希链完整，无异常[/green]")
        return
    console.print(f"[red]发现 {len(issues)} 个问题:[/red]")
    for issue in issues:
        console.print(f"  行 {issue['line']}: {issue['error']}")
    raise typer.Exit(1)


@app.command()
def eval_run(
    model: str = typer.Option("", "--model", "-m", help="评测用模型（默认从配置读取）"),
    cases: str = typer.Option("", "--cases", "-c", help="自定义用例 YAML 路径"),
) -> None:
    asyncio.run(_run_eval(model, cases, console))


async def _run_eval(model: str, cases: str, console: Console) -> None:
    from bandi.core.config import BandiConfig
    from bandi.eval.judge import BenchmarkRunner
    from bandi.llm.mock import MockProvider
    from bandi.llm.registry import ProviderRegistry

    cfg = BandiConfig.load()
    if not model:
        model = cfg.llm.default_model or "deepseek-v4.1-flash"

    if model == "mock" or not cfg.llm.api_key:
        console.print(
            "[yellow]Mock 模式 — 仅验证评测管道（设置 BANDI_API_KEY 后可用真实 LLM）[/yellow]"
        )
        provider = MockProvider()
        judge_provider = MockProvider()
    else:
        reg = ProviderRegistry(cfg)
        provider = reg.get_provider(model, cfg)
        judge_provider = reg.get_provider(
            "deepseek-v4.1-flash" if "deepseek" in model else model, cfg
        )
        console.print(f"[green]使用模型 {model} 进行评测[/green]")
        console.print("[dim]Judge 将使用真实 LLM 评估基准用例[/dim]")

    test_cases = BenchmarkRunner.load_test_cases(cases) if cases else None
    runner = BenchmarkRunner(judge_provider=judge_provider)
    agent_fn = _make_benchmark_agent(provider)
    report = await runner.run_suite(agent_fn, test_cases=test_cases)
    runner.print_report(report)


_BENCH_TMP_DIR: str | None = None


def _bench_isolated_home() -> str:
    """评测必须隔离数据目录——避免写生产 bandi.db/记忆/偏好库。"""
    global _BENCH_TMP_DIR
    if _BENCH_TMP_DIR is None:
        import tempfile

        _BENCH_TMP_DIR = tempfile.mkdtemp(prefix="bandi-bench-")
    return _BENCH_TMP_DIR


def _make_benchmark_agent(provider: BaseProvider) -> Callable[[str], Awaitable[AgentOutput]]:
    async def agent_fn(user_input: str) -> AgentOutput:
        from pathlib import Path
        from unittest.mock import patch

        from bandi.core.agent_runner import run_agent_turn

        with patch("pathlib.Path.home", return_value=Path(_bench_isolated_home())):
            result = await run_agent_turn(
                user_input=user_input,
                mode="execute",
                entity_store=None,
                stream_handler=None,
            )
        return _BenchmarkAgentOutput(
            final_message=result.reply,
            execution_trace=f"mode={result.terminal_reason}, tokens={result.total_tokens}, turns={result.turn_index}",
        )

    return agent_fn


def validate_serve_host(host: str) -> None:
    """.7：监听地址守卫——非 loopback 监听必须已配置 BANDI_API_KEY。

    否则拒绝启动：本机 Agent 的 API 面无凭证暴露到局域网/公网 = 任意人可驱动
    工具执行（含 bash）。fail-closed，错误信息给出修复指引。
    """
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    if not os.environ.get("BANDI_API_KEY", ""):
        raise RuntimeError(
            f"拒绝在 {host} 启动：非 loopback 监听必须设置 BANDI_API_KEY 环境变量"
            "（否则无凭证暴露 Agent API）。可设置后重试，或改用 --host 127.0.0.1。"
        )


# ==================== Sidecar 动态端口====================
# 功能：桌面壳（Electron）spawn 本进程为 sidecar 时用 `--port 0` 动态取端口，
# 实际端口写回两处供壳读取：
#   ① stdout 结构化行 `BANDI_SIDECAR_READY port=<N> pid=<P>`（最快通道）；
#   ② `{BANDI_HOME}/sidecar.port`（JSON：port/pid/started_at，原子写，壳轮询兜底）。
# 退出（正常/信号）时清理端口文件，避免壳读到陈旧端口连错进程。

SIDECAR_READY_PREFIX = "BANDI_SIDECAR_READY"


def sidecar_port_file() -> Path:
    """功能：sidecar 端口回写文件路径（BANDI_HOME 可覆盖，与 api/app.py 同口径）。"""
    home = Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi")))
    return home / "sidecar.port"


def _write_sidecar_port(port: int, pid: int) -> None:
    """功能：原子写回 sidecar 端口（tmp + replace），读方永远不会看到半截 JSON。"""
    target = sidecar_port_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"port": port, "pid": pid, "started_at": time.time()})
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def _clear_sidecar_port() -> None:
    """功能：退出时清理端口文件；不存在时静默（幂等）。"""
    try:
        sidecar_port_file().unlink()
    except OSError:
        pass


def _serve_sidecar(fastapi_app: object, host: str) -> None:  # pragma: no cover
    """功能：sidecar 模式入口——uvicorn 动态绑定 + 端口回写（`--port 0` 触发）。

    与普通 `uvicorn.run` 的差异仅是端口回写；信号处理、优雅退出仍由
    uvicorn.Server 原生提供。端口文件清理挂 atexit + finally 双保险。

    兼容：uvicorn >= 0.39 `Server.startup` 为 async（本仓 0.51 实测），早期为
    同步 def——运行时探测后注入对应 override，两种版本都能回写端口。
    """
    import inspect
    import socket as _socket

    import uvicorn

    class _SidecarServer(uvicorn.Server):
        """功能：startup 完成后读真实监听端口并回写（兼容新旧 servers 结构）。"""

        def _emit_sidecar_port(self) -> None:
            try:
                port: int | None = None
                for srv in getattr(self, "servers", None) or []:
                    # 旧版 uvicorn：servers 即 socket 列表；新版：Server 对象（.sockets 成员）
                    for sock in getattr(srv, "sockets", None) or [srv]:
                        name = sock.getsockname()
                        if len(name) >= 2 and isinstance(name[1], int) and name[1] > 0:
                            port = int(name[1])
                            break
                    if port is not None:
                        break
            except Exception:  # noqa: BLE001 — 回写失败不阻断服务，壳还有探针兜底
                return
            if port is None:
                return
            _write_sidecar_port(port, os.getpid())
            sys.stdout.write(f"{SIDECAR_READY_PREFIX} port={port} pid={os.getpid()}\n")
            sys.stdout.flush()

        if inspect.iscoroutinefunction(uvicorn.Server.startup):

            async def startup(self, sockets: list[_socket.socket] | None = None) -> None:  # type: ignore[override]
                await uvicorn.Server.startup(self, sockets)  # type: ignore[arg-type,func-returns-value]
                self._emit_sidecar_port()

        else:  # pragma: no cover — 旧版 uvicorn（同步 startup）路径

            def startup(self, sockets: list[_socket.socket] | None = None) -> None:  # type: ignore[override]
                uvicorn.Server.startup(self, sockets)  # type: ignore[arg-type]
                self._emit_sidecar_port()

    config = uvicorn.Config(fastapi_app, host=host, port=0, log_level="info")
    server = _SidecarServer(config)
    atexit.register(_clear_sidecar_port)
    try:
        server.run()
    finally:
        _clear_sidecar_port()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", "-p", help="监听端口（0 = sidecar 动态端口）"),
    start_scheduler: bool = typer.Option(
        True, "--start-scheduler/--no-start-scheduler", help="启动后台定时任务调度器"
    ),
) -> None:
    import uvicorn

    from bandi.api.app import app as fastapi_app

    validate_serve_host(host)
    from bandi.core.instance_lock import InstanceLock, InstanceLockError

    lock = InstanceLock(Path.home() / ".bandi" / "bandi.lock")
    try:
        lock.acquire()
    except InstanceLockError:
        console.print(
            "[red]另一个 Bandi 后端已在使用 ~/.bandi（单实例锁被占用）。[/red]\n"
            "请先关闭桌面应用或已运行的 bandi serve，避免会话/组织/工作项被双进程写坏。"
        )
        raise typer.Exit(1) from None
    os.environ["BANDI_SCHEDULER_AUTOSTART"] = "1" if start_scheduler else "0"
    _trigger_models_dev_sync()
    try:
        if port == 0:
            console.print("[green]以 sidecar 模式启动：动态端口（--port 0）[/green]")
            _serve_sidecar(fastapi_app, host)
            return
        console.print(f"[green]启动 Bandi API 服务器: http://{host}:{port}[/green]")
        uvicorn.run(fastapi_app, host=host, port=port)
    finally:
        lock.release()


@session_app.command("list")
def session_list() -> None:
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[yellow]数据库不存在，无会话记录[/yellow]")
        return
    store = SessionStore(str(db_path))
    sessions = store.list_sessions(limit=20)
    if not sessions:
        console.print("[dim]无活跃会话[/dim]")
        return
    from rich.table import Table

    table = Table(title="会话列表")
    table.add_column("ID", style="cyan")
    table.add_column("标题", style="green")
    table.add_column("模式", style="yellow")
    table.add_column("创建时间")
    for s in sessions:
        table.add_row(
            s["id"][:12], s.get("title", "") or "", s.get("mode", ""), s.get("created_at", "")[:19]
        )
    console.print(table)


@session_app.command("show")
def session_show(
    session_id: str = typer.Argument(help="会话 ID"),
    at: str = typer.Option("", "--at", "-a", help="预览恢复点 entry_id（含自身截断，不删数据）"),
) -> None:
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在[/red]")
        raise typer.Exit(1)
    store = SessionStore(str(db_path))
    session = store.get_session(session_id)
    if not session:
        console.print(f"[red]会话 {session_id} 不存在[/red]")
        raise typer.Exit(1)
    try:
        entries = store.resolved_branch(session_id, at=at or None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]会话:[/bold] {session_id}")
    console.print(f"[bold]标题:[/bold] {session.get('title', '')}")
    console.print(f"[bold]模式:[/bold] {session.get('mode', '')}")
    console.print(
        f"[bold]消息数:[/bold] {len([e for e in entries if e.get('entry_type') == 'message'])}"
    )


@session_app.command("export")
def session_export(
    session_id: str = typer.Argument(help="会话 ID"),
    output: str = typer.Option("", "--output", "-o", help="输出路径"),
) -> None:
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在[/red]")
        raise typer.Exit(1)
    store = SessionStore(str(db_path))
    out_path = output or f"bandi-session-{session_id[:8]}.jsonl"
    count = store.export_jsonl(session_id, out_path)
    console.print(f"[green]已导出 {count} 条记录至 {out_path}[/green]")


@session_app.command("delete")
def session_delete(session_id: str = typer.Argument(help="会话 ID")) -> None:
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在[/red]")
        raise typer.Exit(1)
    store = SessionStore(str(db_path))
    if store.delete_session(session_id):
        console.print(f"[green]已删除会话 {session_id}[/green]")
    else:
        console.print(f"[red]会话 {session_id} 不存在[/red]")
        raise typer.Exit(1)


def _open_session_store() -> SessionStore:
    """打开生产会话库，不存在则退出。"""
    from bandi.memory.session.store import SessionStore

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[red]数据库不存在[/red]")
        raise typer.Exit(1)
    return SessionStore(str(db_path))


@session_app.command("fork")
def session_fork(
    session_id: str = typer.Argument(help="会话 ID"),
    entry: str = typer.Option("", "--entry", "-e", help="恢复点 entry_id（后缀也可，自动全匹配）"),
) -> None:
    """Fork：复制恢复点前缀到独立新会话并记录溯源。"""
    from bandi.session.manager import SessionManager

    store = _open_session_store()
    try:
        if store.get_session(session_id) is None:
            console.print(f"[red]会话 {session_id} 不存在[/red]")
            raise typer.Exit(1)
        manager = SessionManager(store)
        manager.session_id = session_id
        entries = store.get_branch(session_id)
        if not entries:
            console.print("[red]会话无条目，无法 fork[/red]")
            raise typer.Exit(1)
        if not entry:
            console.print("[red]需提供 --entry 恢复点[/red]")
            raise typer.Exit(1)
        target = next(
            (e["id"] for e in entries if e["id"] == entry or e["id"].startswith(entry)),
            None,
        )
        if target is None:
            console.print(f"[red]恢复点 {entry} 不在会话 {session_id} 中[/red]")
            raise typer.Exit(1)
        new_sid = manager.fork(target)
        console.print(f"[green]已 fork 至新会话 {new_sid}[/green]")
    finally:
        store.close()


@session_app.command("revert")
def session_revert(
    session_id: str = typer.Argument(help="会话 ID"),
    entry: str = typer.Option(
        "", "--entry", "-e", help="Stage 恢复点 entry_id（与 --action stage 搭配）"
    ),
    action: str = typer.Option(
        "",
        "--action",
        "-a",
        help="操作: stage / commit / clear（默认 commit）",
    ),
) -> None:
    """Revert 三阶段：stage 暂存恢复点 / commit 永久截断 / clear 撤销。"""
    from bandi.session.manager import SessionManager

    store = _open_session_store()
    try:
        if store.get_session(session_id) is None:
            console.print(f"[red]会话 {session_id} 不存在[/red]")
            raise typer.Exit(1)
        manager = SessionManager(store)
        manager.session_id = session_id
        act = action or "commit"
        try:
            if act == "stage":
                if not entry:
                    console.print("[red]stage 需提供 --entry 恢复点[/red]")
                    raise typer.Exit(1)
                matches = [
                    e["id"]
                    for e in store.get_branch(session_id)
                    if e["id"] == entry or e["id"].startswith(entry)
                ]
                if not matches:
                    console.print(f"[red]恢复点 {entry} 不在会话中[/red]")
                    raise typer.Exit(1)
                manager.stage(matches[0])
                console.print(f"[green]已暂存恢复点 {matches[0]}[/green]")
            elif act == "commit":
                removed = manager.commit()
                console.print(f"[green]已截断 {removed} 条条目到恢复点[/green]")
            elif act == "clear":
                manager.clear()
                console.print("[green]已撤销暂存恢复点[/green]")
            else:
                console.print(f"[red]未知操作 {act}[/red]")
                raise typer.Exit(1)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    finally:
        store.close()


@session_app.command("revert-files")
def session_revert_files(
    session_id: str = typer.Argument(help="会话 ID"),
    turn: int = typer.Option(
        ..., "--turn", "-t", help="回滚到该轮开始前（撤销该轮及之后的文件编辑）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览不改动"),
) -> None:
    """文件级回滚：按轮恢复工作区文件（编辑前自动备份）。"""
    from bandi.agent.file_history import get_file_history_store

    store = _open_session_store()
    try:
        if store.get_session(session_id) is None:
            console.print(f"[red]会话 {session_id} 不存在[/red]")
            raise typer.Exit(1)
        report = get_file_history_store().rewind(session_id, turn, dry_run=dry_run)
        if not report.actions:
            console.print("[yellow]该轮及之后没有文件编辑记录[/yellow]")
            return
        styles = {
            "restore": "green",
            "delete": "red",
            "skip": "dim",
            "unavailable": "yellow",
        }
        for action in report.actions:
            style = styles.get(action.action, "")
            console.print(f"[{style}]{action.action:11s}[/{style}] {action.path}")
        label = "预览完成" if dry_run else "回滚完成"
        suffix = "将变更" if dry_run else "已变更"
        console.print(f"[green]{label}：{len(report.changed)} 个文件{suffix}[/green]")
    finally:
        store.close()


def _get_ontology_store():
    from pathlib import Path

    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    db_path = Path.home() / ".bandi" / "data" / "ontology.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return OntologyStore(open_ontology(str(db_path)))


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@entity_app.command("list")
def entity_list(
    class_name: str | None = typer.Option(None, "--class", "-c", help="按类过滤（含其子类）"),
) -> None:
    from bandi.ontology.engine import query_entities

    store = _get_ontology_store()
    try:
        graph = store.graph()
        found = query_entities(graph, {"class_id": class_name} if class_name else {})
        if not found:
            console.print("[dim]无实体[/dim]")
            return
        from rich.table import Table

        table = Table(title="实体列表")
        table.add_column("ID", style="cyan")
        table.add_column("类", style="green")
        table.add_column("名称", style="white")
        for e in found:
            table.add_row(e.id, "、".join(e.classes) or "-", e.label or e.id)
        console.print(table)
    finally:
        store.close()


@entity_app.command("get")
def entity_get(entity_id: str = typer.Argument(help="实体 ID")) -> None:
    store = _get_ontology_store()
    try:
        entity = store.graph().entities.get(entity_id)
        if not entity:
            console.print(f"[red]实体 {entity_id} 不存在[/red]")
            raise typer.Exit(1)
        from rich.panel import Panel

        panel = Panel(
            f"[bold]名称:[/bold] {entity.label or entity.id}\n"
            f"[bold]类:[/bold] {'、'.join(entity.classes) or '-'}\n"
            f"[bold]注释:[/bold] {entity.comment or '-'}",
            title=f"实体 {entity_id}",
        )
        console.print(panel)
    finally:
        store.close()


@entity_app.command("create")
def entity_create(
    class_name: str = typer.Argument(help="类名（须已定义，如 SalesRep）"),
    name: str = typer.Argument(help="实体名称（中文可）"),
    entity_id: str = typer.Option("", "--id", help="实体 ID（ASCII，可选，自动生成）"),
) -> None:
    import uuid

    store = _get_ontology_store()
    try:
        eid = entity_id or f"ent-{uuid.uuid4().hex[:8]}"
        outcome = store.assert_entity(
            {"id": eid, "classes": [class_name] if class_name else [], "label": name},
            _now_iso(),
        )
        if not outcome.ok:
            console.print("[red]写入被拒:[/red]")
            for violation in outcome.violations:
                console.print(f"  - {violation.message}")
            raise typer.Exit(1)
        console.print(f"[green]已创建实体 {eid}（{name}）[/green]")
    finally:
        store.close()


@entity_app.command("delete")
def entity_delete(entity_id: str = typer.Argument(help="实体 ID（级联删除其全部事实）")) -> None:
    store = _get_ontology_store()
    try:
        outcome = store.retract_entity(entity_id)
        if outcome.removed:
            console.print(
                f"[green]已删除实体 {entity_id}（级联 {outcome.facts_removed} 条事实）[/green]"
            )
        else:
            console.print(f"[red]实体 {entity_id} 不存在[/red]")
            raise typer.Exit(1)
    finally:
        store.close()


def _get_badcase_tracker():
    from pathlib import Path

    from bandi.eval.badcase_tracker import BadCaseTracker
    from bandi.safety.pii_detector import PIIDetector

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    if not db_path.exists():
        console.print("[yellow]数据库不存在，无 badcase 记录[/yellow]")
        raise typer.Exit(0)
    conn = connect(db_path, row_factory=True)
    return BadCaseTracker(conn, masker=PIIDetector().sanitize)


@badcase_app.command("list")
def badcase_list(
    type: str | None = typer.Option(None, "--type", "-t", help="失败类型过滤"),
    limit: int = typer.Option(20, "--limit", "-n", help="最大条数"),
) -> None:
    bt = _get_badcase_tracker()
    cases = bt.list(failure_type=type, limit=limit)
    if not cases:
        console.print("[dim]无 badcase 记录[/dim]")
        return
    from rich.table import Table

    table = Table(title=f"Badcase 列表 (type={type or '*'}, limit={limit})")
    table.add_column("ID", style="cyan")
    table.add_column("类型", style="red")
    table.add_column("严重度", style="yellow")
    table.add_column("详情", style="white")
    for c in cases:
        table.add_row(c.id[:8], c.failure_type, c.severity, c.detail[:40])
    console.print(table)


@badcase_app.command("stats")
def badcase_stats() -> None:
    bt = _get_badcase_tracker()
    s = bt.stats()
    from rich.panel import Panel

    by_type = "\n".join(f"  {k}: {v}" for k, v in s["by_type"].items())
    by_severity = "\n".join(f"  {k}: {v}" for k, v in s["by_severity"].items())
    panel = Panel(
        f"[bold]总计:[/bold] {s['total']}\n"
        f"[bold]未解决:[/bold] {s['unresolved']}\n"
        f"[bold]按类型:[/bold]\n{by_type}\n"
        f"[bold]按严重度:[/bold]\n{by_severity}",
        title="Badcase 统计",
    )
    console.print(panel)


@app.command()
def maintain(
    ttl_days: int = typer.Option(0, "--ttl-days", help="会话保留天数（0 = 用配置值）"),
    forget_days: int = typer.Option(0, "--forget-days", help="记忆遗忘阈值天数（0 = 用配置值）"),
    promote_limit: int = typer.Option(0, "--promote-limit", help="单次晋升条数（0 = 用配置值）"),
) -> None:
    """功能：手动执行记忆/会话生命周期维护（会话过期清理 + 归档 + 遗忘 + 晋升）。

    维护此前**只**挂在 API 调度器上（`/api/scheduler/start`），CLI 用户
    永远不触发 → 情节/会话/草稿无界增长。此命令给 CLI 一条显式入口，参数口径与
    API 装配（`memory_maintenance` 配置）同源，未显式给时用同一份配置值。
    """
    from bandi.core.config import BandiConfig
    from bandi.memory.maintenance import default_maintenance_callable

    maintenance = BandiConfig.load().memory_maintenance
    run_maintenance = default_maintenance_callable(
        ttl_days=int(ttl_days or maintenance.session_ttl_days),
        forget_older_than_days=int(forget_days or maintenance.forget_older_than_days),
        promote_limit=int(promote_limit or maintenance.promote_limit),
    )
    try:
        result = run_maintenance()
    except Exception as e:  # 维护失败不得伪装成成功
        from bandi.utils.errors import error_text

        console.print(f"[red]维护执行失败：{error_text(e)}[/red]")
        raise typer.Exit(1) from e
    from rich.panel import Panel

    summary = "\n".join(f"  {k}: {v}" for k, v in sorted(result.items()))
    console.print(Panel(summary or "（无待处理条目）", title="记忆维护完成"))


if __name__ == "__main__":
    app()
