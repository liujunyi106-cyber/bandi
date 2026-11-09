# 功能：公司模式 CLI（init/roles/recruit/status/reorg/add-role/add-employee/tick/kanban 命令）
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.org_persistence import (
    load_org,
    org_json_path,
    save_org,
)
from bandi.company.recruiter import Recruiter
from bandi.company.reorg_manager import ReorgChangeSet, ReorgManager, ReorgRoleChange
from bandi.company.store import WorkItemStore

if TYPE_CHECKING:
    from bandi.company.company_mode import CompanyDispatcher

console = Console()
company_app = typer.Typer(help="Company Mode 组织管理")
_engine = OrgEngine()
_recruiter = Recruiter()
_reorg_manager: ReorgManager | None = None
_reorg_store: WorkItemStore | None = None
# 组织本体（自定义角色/员工）持久化路径——CLI 进程退出不丢
_org_db_path = str(org_json_path())
# degraded 告警在生产路径永不生效）
_engine._persist_callable = lambda: save_org(_engine, _recruiter, _org_db_path)


def _save_org() -> None:
    """将自定义角色/员工/席位状态写入 JSON（原子写，委托共享模块）。"""
    save_org(_engine, _recruiter, _org_db_path)


def _load_persisted_org() -> None:
    """从 org.json 加载自定义角色/员工/席位状态（幂等，委托共享模块）。"""
    load_org(_engine, _recruiter, _org_db_path)


def _reorg() -> ReorgManager:
    global _reorg_manager, _reorg_store
    if _reorg_manager is None:
        # 与 _dispatcher 同库（company.db）——reorg 任务迁移必须看到真实工作项
        # 旧 company_reorg.db 是空库，迁移计划恒 0 任务静默失效
        db_path = Path.home() / ".bandi" / "company" / "company.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _reorg_store = WorkItemStore(str(db_path))
        _reorg_manager = ReorgManager(store=_reorg_store, org_engine=_engine)
    return _reorg_manager


def _dispatcher() -> tuple[CompanyDispatcher, WorkItemStore]:
    """懒加载 CompanyDispatcher 单例（~/.bandi/company/company.db）。

    与 api/app.py 的 _get_company_dispatcher 保持同一数据目录与装配
    使 CLI tick/kanban 与 API 端点共享同一 store。
    """
    from bandi.company.communication import CommunicationManager
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.external_seat_executor import ExternalSeatExecutor
    from bandi.company.gate_harness import GateHarness
    from bandi.company.seat_executor import EngineSeatExecutor

    def _dispatcher_config():
        # 功能：并发/预算取配置（配置不可用时回落 DispatcherConfig 默认值）

        try:
            from bandi.core.config import BandiConfig

            company = BandiConfig.load().company
        except Exception:  # noqa: BLE001 — CLI 装配不因配置异常失败
            return DispatcherConfig()
        return DispatcherConfig(
            max_concurrent=company.max_concurrent,
            tick_budget_sec=company.tick_budget_sec,
            max_items_per_tick=company.max_items_per_tick,
        )

    db_path = Path.home() / ".bandi" / "company" / "company.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = WorkItemStore(str(db_path))
    # 复用模块级组织（已加载持久化角色/员工）
    org = _engine
    recruiter = _recruiter
    if not recruiter.get_seat("worker"):
        recruiter.register_seat("worker")
    # 装配通信管理器——工作项内协作工具（send_dm/inbox/route_work 等 9 个）
    # 经 EngineSeatExecutor 透传给 AgentLoop，company 链路不 fail-closed
    communication = CommunicationManager(
        workspace=str(db_path.parent),
        project_id="company",
        session_id="default",
        org_engine=org,
    )
    # 线程容器装配——与 api/app.py 同一 workspace，CLI tick 产出回写共享线程目录
    from bandi.company.discussion_thread import ThreadManager

    thread_manager = ThreadManager(
        workspace=str(db_path.parent),
        project_id="company",
        session_id="default",
    )
    thread_manager.hydrate()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        communication=communication,
        # 装配 seat_executor——run_work_item 执行工作项所需；store/thread_manager 供
        # spawn_thread_task 装配接线（EngineSeatExecutor→run_agent→run_agent_turn）
        seat_executor=EngineSeatExecutor(
            communication=communication,
            org_engine=org,
            store=store,
            thread_manager=thread_manager,
        ),
        # 装配：外部引擎一次性执行路径（真外聘 ）
        external_seat_executor=ExternalSeatExecutor(),
        # 并发/预算与 API 装配同源（配置项，默认并发 3）
        config=_dispatcher_config(),
        thread_manager=thread_manager,
    )
    # 持有效租约（execution_claim），CLI 眼里它们是 live 的；租约过期的
    # 遗留卡由 API 服务启动时的 recover 接管（live 拒绝修复原则）
    return dispatcher, store


_load_persisted_org()


@company_app.command()
def init(
    ceo_name: str = typer.Option("CEO", help="CEO 角色名称"),
    worker_count: int = typer.Option(3, help="Worker 数量"),
) -> None:
    _engine.register_role("ceo", ceo_name, "Top-level decision maker")
    _recruiter.register_seat("ceo")
    for stale in [r.role_id for r in _engine.list_agents() if r.role_id.startswith("worker_")]:
        _engine.remove_role(stale)
    for i in range(worker_count):
        role_id = f"worker_{i + 1}"
        _engine.register_role(role_id, f"Worker {i + 1}", "Executes tasks", reports_to="ceo")
        _recruiter.register_seat(role_id)
    _save_org()
    console.print(f"[green]Company initialized: 1 CEO + {worker_count} Workers[/green]")


@company_app.command()
def roles() -> None:
    agents = _engine.list_agents()
    if not agents:
        console.print("[yellow]No roles registered. Run 'bandi company init' first.[/yellow]")
        return
    table = Table(title="Company Roles")
    table.add_column("Role ID", style="cyan")
    table.add_column("Name")
    table.add_column("Reports To")
    for agent in agents:
        table.add_row(agent.role_id, agent.name, agent.reports_to or "-")
    console.print(table)


@company_app.command()
def recruit(role_id: str = typer.Argument(..., help="角色 ID")) -> None:
    seat = _recruiter.recruit(role_id)
    if seat:
        _save_org()
        console.print(f"[green]Recruited {role_id}[/green]")
    else:
        console.print(f"[red]Failed to recruit {role_id}: not available[/red]")


@company_app.command()
def status() -> None:
    available = len(_recruiter.list_available())
    recruited = len(_recruiter.list_recruited())
    console.print(f"[cyan]Available:[/cyan] {available}")
    console.print(f"[cyan]Recruited:[/cyan] {recruited}")


@company_app.command()
def add_role(
    role_id: str = typer.Argument(..., help="角色 ID"),
    name: str = typer.Option("", "--name", help="角色名称"),
    reports_to: str = typer.Option(None, "--reports-to", help="上级角色 ID"),
    can_spawn: list[str] = typer.Option([], "--can-spawn", help="可派生角色（可重复）"),
    responsibility: str = typer.Option("", "--responsibility", help="职责描述"),
    is_final_decider: bool = typer.Option(False, "--final-decider", help="设为最终决策角色"),
) -> None:
    """手动创建角色（组织配置，非 LLM 招募）。"""
    from bandi.company.employee_lifecycle import EmployeeTombstoneStore

    if EmployeeTombstoneStore().get(role_id) is not None:
        console.print("[red]IDENTITY_RESERVED[/red]")
        raise typer.Exit(code=1)
    try:
        _engine.register_role(
            role_id,
            name or role_id,
            responsibility,
            reports_to,
            can_spawn,
            is_final_decider=is_final_decider,
        )
    except ValueError as e:
        from bandi.utils.errors import error_text

        console.print(f"[red]{error_text(e)}[/red]")
        raise typer.Exit(code=1)
    _save_org()
    console.print(f"[green]Role '{role_id}' registered[/green]")


@company_app.command()
def add_employee(
    employee_id: str = typer.Argument(..., help="员工 ID"),
    name: str = typer.Option("", "--name", help="员工名称"),
    role: str = typer.Option(..., "--role", help="角色 ID"),
    description: str = typer.Option("", "--description", help="描述"),
) -> None:
    """手动创建员工（绑定角色，非 LLM 招募）。"""
    from bandi.company.employee_lifecycle import EmployeeTombstoneStore

    if _engine.get_role(role) is None:
        console.print(f"[red]Unknown role '{role}'[/red]")
        raise typer.Exit(code=1)
    tombstones = EmployeeTombstoneStore()
    if tombstones.get(employee_id) is not None or tombstones.get(role) is not None:
        console.print("[red]IDENTITY_RESERVED[/red]")
        raise typer.Exit(code=1)
    if _engine.get_employee(employee_id) is not None:
        console.print(f"[red]Employee '{employee_id}' already exists[/red]")
        raise typer.Exit(code=1)
    occupant = next(
        (
            item
            for item in _engine.list_employees(include_inactive=True)
            if str(getattr(item, "role_id", "") or "") == role
            and str(getattr(item, "status", "active")) in {"active", "paused"}
        ),
        None,
    )
    if occupant is not None:
        console.print(f"[red]Role '{role}' already staffed by {occupant.employee_id}[/red]")
        raise typer.Exit(code=1)
    _engine.register_employee(
        OrgEmployee(
            employee_id=employee_id,
            name=name or employee_id,
            role_id=role,
            description=description,
            llm_config=None,  # D-3：CLI 手工创建默认跟随全局配置（llm_config 路由留 API/招募面）
        )
    )
    _save_org()
    console.print(f"[green]Employee '{employee_id}' registered with role '{role}'[/green]")


@company_app.command()
def reorg(
    replace_specs: list[str] = typer.Option([], "--replace", help="角色替换 old=new（可重复）"),
    remove_roles: list[str] = typer.Option([], "--remove-role", help="移除角色（可重复）"),
    summary: str = typer.Option("", "--summary", help="变更说明"),
) -> None:
    """运行时组织重构：角色替换/移除，自动迁移受影响任务与员工。"""
    role_changes: list[ReorgRoleChange] = []
    for spec in replace_specs:
        if "=" not in spec:
            console.print(f"[red]Invalid --replace '{spec}': expected old=new[/red]")
            return
        old_role, new_role = spec.split("=", 1)
        role_changes.append(
            ReorgRoleChange(
                action="replace",
                role_id=old_role,
                replacement_role_id=new_role,
                role={"id": new_role},
            )
        )
    for role_id in remove_roles:
        role_changes.append(ReorgRoleChange(action="remove", role_id=role_id))
    if not role_changes:
        console.print(
            "[yellow]Nothing to do. Use --replace old=new or --remove-role <id>.[/yellow]"
        )
        return

    manager = _reorg()
    proposal = manager.propose_reorg(
        project_id="default",
        summary=summary or "CLI reorg",
        changeset=ReorgChangeSet(role_changes=role_changes),
    )
    manager.set_reorg_approval(
        proposal.proposal_id, approved=True, notes="CLI approved", source="cli"
    )
    result = manager.apply_reorg(proposal.proposal_id)
    summary_parts = result["migration_summary"]
    console.print(
        f"[green]Applied reorg {proposal.proposal_id}[/green]: "
        f"{len(summary_parts['migrated_task_ids'])} tasks migrated, "
        f"{len(summary_parts['migrated_employee_ids'])} employees migrated, "
        f"{len(summary_parts['retired_employee_ids'])} employees retired"
    )


@company_app.command()
def tick() -> None:
    """驱动一轮调度 tick，打印调度报告。"""
    dispatcher, _store = _dispatcher()
    report = asyncio.run(dispatcher.tick())
    console.print(f"[cyan]tick {report.tick_id[:8]}[/cyan]")
    console.print(f"[cyan]processed:[/cyan] {len(report.processed)}")
    console.print(f"[cyan]claimed:[/cyan] {len(report.claimed)}")
    console.print(f"[cyan]released:[/cyan] {len(report.released)}")
    if report.transitions:
        table = Table(title="Transitions")
        table.add_column("Work Item")
        table.add_column("From")
        table.add_column("To")
        for item_id, old_phase, new_phase in report.transitions:
            table.add_row(item_id[:8], old_phase, new_phase)
        console.print(table)
    if report.errors:
        for error in report.errors:
            console.print(f"[yellow]{error}[/yellow]")


@company_app.command()
def run(
    wait_timeout_sec: float = typer.Option(30.0, "--wait-timeout", help="空闲轮询超时（秒）"),
) -> None:
    """常驻调度循环（wake 驱动 tick + 周期心跳 + sweeper）——Ctrl+C 停止。"""
    dispatcher, _store = _dispatcher()
    state = {"running": True}

    def _running() -> bool:
        return state["running"]

    try:
        asyncio.run(dispatcher.run_forever(_running, wait_timeout_sec=wait_timeout_sec))
    except KeyboardInterrupt:
        state["running"] = False


@company_app.command()
def kanban() -> None:
    """打印当前工作项的 kanban 列分组。"""
    dispatcher, _store = _dispatcher()
    snapshot = dispatcher.get_report()
    if not snapshot.column_counts:
        console.print("[yellow]No work items[/yellow]")
        return
    table = Table(title="Kanban")
    table.add_column("Column")
    table.add_column("Count")
    for column, count in sorted(snapshot.column_counts.items()):
        table.add_row(column, str(count))
    console.print(table)


@company_app.command()
def review(
    work_item_id: str = typer.Argument(..., help="工作项 ID"),
    verdict: str = typer.Argument(..., help="评审结论：approve / rework"),
    feedback: str = typer.Option("", "--feedback", help="返工反馈（verdict=rework 时建议填写）"),
) -> None:
    """评审决策注入：写 review_verdict 解除 AWAITING_MANAGER_REVIEW/AWAITING_HUMAN 滞留。"""
    dispatcher, _store = _dispatcher()
    try:
        dispatcher.review_work_item(work_item_id, verdict, feedback=feedback)
    except (KeyError, ValueError) as e:
        from bandi.utils.errors import error_text

        console.print(f"[red]{error_text(e)}[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Review {verdict} recorded for {work_item_id[:8]}[/green]"
        + (f" (feedback: {feedback})" if feedback else "")
    )


@company_app.command()
def retry(
    work_item_id: str = typer.Argument(..., help="工作项 ID"),
) -> None:
    """重试 NEEDS_ATTENTION 工作项：转 READY 重新入队调度（输出契约违规卡人工恢复）。"""
    dispatcher, _store = _dispatcher()
    try:
        dispatcher.retry_work_item(work_item_id)
    except (KeyError, ValueError) as e:
        from bandi.utils.errors import error_text

        console.print(f"[red]{error_text(e)}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Retry queued for {work_item_id[:8]}[/green]")


@company_app.command()
def collab_dispatch(
    tool_name: str = typer.Argument(..., help="协作工具名称（inbox/send_dm 等）"),
    args_json: str = typer.Option("", "--args-json", help="工具参数 JSON 字符串"),
) -> None:
    """调用协作工具（同进程直调 dispatch，无需 RPC broker）。"""
    import json as _json

    from bandi.tools.collaboration_dispatch import dispatch_collaboration_tool

    role = os.environ.get("BANDI_COMMS_FROM", "")
    if not role:
        console.print(
            "[red]Missing BANDI_COMMS_FROM: set BANDI_COMMS_FROM to use collab-dispatch[/red]"
        )
        raise typer.Exit(1)

    args: dict[str, Any] = {}
    if args_json:
        try:
            args = _json.loads(args_json)
        except _json.JSONDecodeError as exc:
            console.print(f"[red]Invalid --args-json: {exc}[/red]")
            raise typer.Exit(1)

    env: dict[str, str] = {
        "BANDI_WORKSPACE_ROOT": os.environ.get("BANDI_WORKSPACE_ROOT", ""),
        "BANDI_COMMS_PROJECT": os.environ.get("BANDI_COMMS_PROJECT", "company"),
        "BANDI_COMMS_SESSION": os.environ.get("BANDI_COMMS_SESSION", "default"),
        "BANDI_COMMS_FROM": role,
    }
    dispatcher, _store = _dispatcher()
    result, is_error = asyncio.run(
        dispatch_collaboration_tool(tool_name, args, env=env, org_engine=dispatcher.org_engine)
    )
    typer.echo(_json.dumps(result, ensure_ascii=False, default=str))
    if is_error:
        raise typer.Exit(1)


@company_app.command()
def preflight() -> None:
    """启动预检：探测外部 Agent 可用性 + 工作区写入契约 + RPC 传输诊断。"""
    from bandi.company.preflight import run_external_agent_preflight

    results = run_external_agent_preflight(probe_commands=False)
    output = json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=2, default=str)
    typer.echo(output)


@company_app.command("migrate-plan")
def migrate_plan() -> None:
    """功能：dry-run 输出 retired 员工迁移清单。"""
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, str(root / "scripts" / "migrate_retired.py")], check=True)


@company_app.command("migrate-apply")
def migrate_apply() -> None:
    """功能：执行 retired 员工迁移（破坏性，不备份）。"""
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, str(root / "scripts" / "migrate_retired.py"), "--apply"], check=True
    )
