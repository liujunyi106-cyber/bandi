# 功能：团队模板 CLI（list/create/delete/export/import/update-skills/update-sop/instantiate）
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from bandi.company.agent_profile import AgentProfileManager
from bandi.company.org_engine import OrgEngine
from bandi.company.org_persistence import load_org, org_json_path, save_org
from bandi.company.recruiter import Recruiter
from bandi.company.team_template import TeamTemplate, TeamTemplateError
from bandi.company.team_template_store import TeamTemplateStore, build_org_from_template

console = Console()
team_app = typer.Typer(help="团队模板管理")

_store = TeamTemplateStore()
_engine = OrgEngine()
_recruiter = Recruiter()
_org_path = str(org_json_path())
# 注：不在模块导入时绑定 _persist_callable——load_org 期间回调会反复写


def _require_template(template_id: str) -> TeamTemplate:
    try:
        template = _store.get_template(template_id)
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if template is None:
        console.print(f"[red]template not found: {template_id}[/red]")
        raise typer.Exit(code=1)
    return template


@team_app.command("list")
def team_list() -> None:
    """列出全部团队模板（内置 + 本地库）。"""
    table = Table(title="Team Templates")
    table.add_column("template_id")
    table.add_column("name")
    table.add_column("roles")
    table.add_column("source")
    for template in _store.list_templates():
        source = "local" if _store.template_path(template.template_id).is_file() else "builtin"
        table.add_row(template.template_id, template.name, str(len(template.roles)), source)
    console.print(table)


@team_app.command("create")
def team_create(
    template_id: str = typer.Argument(..., help="新模板 id"),
    from_id: str = typer.Option(..., "--from", help="来源模板 id（内置或本地）"),
    name: str = typer.Option("", "--name", help="显示名（缺省沿用来源）"),
) -> None:
    """从既有模板复制创建可编辑副本。"""
    if Path(_store.template_path(template_id)).is_file():
        console.print(f"[red]template already exists: {template_id}[/red]")
        raise typer.Exit(code=1)
    source = _require_template(from_id)
    created = source.model_copy(update={"template_id": template_id, "name": name or source.name})
    try:
        _store.save_template(created)
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"created template `{template_id}` from `{from_id}`")


@team_app.command("delete")
def team_delete(template_id: str = typer.Argument(...)) -> None:
    """删除本地模板（内置不可删）。"""
    try:
        deleted = _store.delete_template(template_id)
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not deleted:
        console.print(f"[red]template not found (or builtin): {template_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"deleted template `{template_id}`")


@team_app.command("export")
def team_export(
    template_id: str = typer.Argument(...),
    out: str = typer.Option(..., "--out", help="导出文件路径"),
) -> None:
    """导出模板为 JSON 文件（内置模板可直接导出分享）。"""
    template = _require_template(template_id)
    try:
        path = _store.export_template(template_id, out)
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"exported `{template.template_id}` to {path}")


@team_app.command("import")
def team_import(
    path: str = typer.Argument(..., help="JSON 模板文件路径"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在模板"),
) -> None:
    """从 JSON 文件导入模板到本地库。"""
    try:
        template = _store.import_template(path, overwrite=overwrite)
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"imported template `{template.template_id}`")


@team_app.command("update-skills")
def team_update_skills(
    template_id: str = typer.Argument(...),
    role: str = typer.Option(..., "--role", help="角色 id"),
    skills: str = typer.Option("", "--skills", help="逗号分隔技能名（空串清空）"),
) -> None:
    """更新角色技能绑定（全量替换）。"""
    template = _require_template(template_id)
    refs = [s.strip() for s in skills.split(",") if s.strip()]
    found = False
    roles = []
    for spec in template.roles:
        if spec.id == role:
            found = True
            roles.append(spec.model_copy(update={"skill_refs": refs}))
        else:
            roles.append(spec)
    if not found:
        console.print(f"[red]role not found: {role}[/red]")
        raise typer.Exit(code=1)
    try:
        _store.save_template(template.model_copy(update={"roles": roles}))
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"updated skills for `{template_id}` role `{role}`: {refs}")


@team_app.command("update-sop")
def team_update_sop(
    template_id: str = typer.Argument(...),
    sop: str = typer.Option("", "--sop", help="逗号分隔 SOP 引用（空串清空）"),
) -> None:
    """更新团队级 SOP 引用（全量替换）。"""
    template = _require_template(template_id)
    refs = [s.strip() for s in sop.split(",") if s.strip()]
    try:
        _store.save_template(template.model_copy(update={"sop_refs": refs}))
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"updated sop refs for `{template_id}`: {refs}")


@team_app.command("instantiate")
def team_instantiate(
    template_id: str = typer.Argument(...),
    team_id: str = typer.Option("", "--team-id", help="覆盖 home team id"),
) -> None:
    """一键实例化：前缀角色 / 员工 1:1 / 席位 / profiles 落组织并持久化（org.json）。

    持久化顺序：先 load（此时未绑回调，无中间态写）→ build →
    末尾一次显式 save_org（含席位——席位注册不触发 OrgEngine._persist
    必须在 build 后统一落盘，否则重启后团队卡不可 claim）。
    """
    template = _require_template(template_id)
    if team_id:
        template = template.model_copy(update={"team_id": team_id})
    load_org(_engine, _recruiter, _org_path)
    try:
        report = build_org_from_template(
            template,
            _engine,
            recruiter=_recruiter,
            profile_manager=AgentProfileManager(),
        )
    except TeamTemplateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    save_org(_engine, _recruiter, _org_path)
    console.print(
        f"instantiated team `{report.team_id}`: "
        f"{len(report.created_role_ids)} roles created / "
        f"{len(report.skipped_role_ids)} skipped / "
        f"{len(report.created_employee_ids)} employees created"
    )
    for warning in report.warnings:
        console.print(f"[yellow]warning: {warning}[/yellow]")
