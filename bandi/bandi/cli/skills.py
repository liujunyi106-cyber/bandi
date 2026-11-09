# 功能：技能管理 CLI——学习型技能版本回滚（.7）+ Skill Card 局部改写（
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from bandi.skills.branching import SkillBranch
    from bandi.skills.runtime import SkillFrame, SkillFrameStack

skills_app = typer.Typer(help="技能管理")

_DEFAULT_SKILLS_DIR = "~/.bandi/skills"
_DEFAULT_FRAMES_DIR = "~/.bandi/skills/frames"
_FRAME_ACTIONS = ("start", "advance", "suspend", "resume", "complete", "status")
_BRANCH_ACTIONS = ("fork", "update", "sync", "promote", "rollback", "versions")


@skills_app.callback()
def _skills_root() -> None:
    """技能管理命令组（保持 bandi skills <command> 二级形态）。"""


@skills_app.command("rollback")
def rollback_cmd(
    skill_name: str = typer.Argument(..., help="技能名（SKILL.md frontmatter name）"),
    version: int = typer.Argument(..., help="目标历史版本号（history/SKILL.vN.md）"),
    skills_dir: str = typer.Option("", "--skills-dir", help="技能库目录（默认 ~/.bandi/skills）"),
) -> None:
    """回滚学习型技能到历史版本（history 快照原子恢复；缺失版本显式失败）。"""
    from bandi.company.employee_evolution import EmployeeEvolutionManager

    manager = EmployeeEvolutionManager(skills_dir=skills_dir or None)
    ok = manager.rollback_skill(skill_name, version)
    typer.echo(
        json.dumps({"skill": skill_name, "version": version, "rolled_back": ok}, ensure_ascii=False)
    )
    if not ok:
        raise typer.Exit(code=1)


@skills_app.command("rewrite")
def rewrite_cmd(
    skill_id: str = typer.Argument(..., help="技能卡标识（<skills_dir>/<skill_id>.card.json）"),
    instruction: str = typer.Option(..., "--instruction", "-i", help="自然语言改写要求"),
    target_path: str = typer.Option("", "--target-path", help="单一目标路径（basic / nodes[0]）"),
    target_paths: list[str] = typer.Option(
        None, "--target-paths", help="目标路径集（可重复；空则用 --target-path）"
    ),
    target_label: str = typer.Option("", "--target-label", help="目标区域展示名"),
    skills_dir: str = typer.Option("", "--skills-dir", help="技能库目录（默认 ~/.bandi/skills）"),
) -> None:
    """局部改写既有技能卡并回写落盘（读回 → 改写 → 反思 → 落盘）。"""
    from bandi.agent.factory import build_provider
    from bandi.core.config import BandiConfig
    from bandi.skills.editor import SkillEditor

    editor = SkillEditor(
        build_provider(BandiConfig.load()),
        skills_dir=skills_dir or _DEFAULT_SKILLS_DIR,
    )
    try:
        response = asyncio.run(
            editor.rewrite_and_persist(
                skill_id,
                instruction,
                target_path=target_path,
                target_paths=list(target_paths or []),
                target_label=target_label,
                session_id=f"skill_rewrite:{uuid.uuid4().hex[:8]}",
            )
        )
    except Exception as exc:
        typer.echo(json.dumps({"skill_id": skill_id, "error": str(exc)}, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "skill_id": skill_id,
                "assistant_message": response.assistant_message,
                "changed_paths": response.changed_paths,
                "warnings": response.warnings,
            },
            ensure_ascii=False,
        )
    )


@skills_app.command("frame")
def frame_cmd(
    scope: str = typer.Option(..., "--scope", help="帧栈作用域标识（会话/员工等）"),
    action: str = typer.Option(..., "--action", help=" / ".join(_FRAME_ACTIONS)),
    skill: str = typer.Option("", "--skill", help="技能卡标识（action=start 必填）"),
    task_id: str = typer.Option("", "--task-id", help="任务帧标识（resume 必填，start 可选）"),
    slot: list[str] = typer.Option(None, "--slot", help="槽位 k=v（可重复；action=start）"),
    next_node: str = typer.Option("", "--next-node", help="显式指定后继节点（action=advance）"),
    skills_dir: str = typer.Option("", "--skills-dir", help="技能库目录（默认 ~/.bandi/skills）"),
    frames_dir: str = typer.Option(
        "", "--frames-dir", help="帧栈目录（默认 ~/.bandi/skills/frames）"
    ),
) -> None:
    """任务帧栈操作（建帧/推进/挂起/恢复/完成/查看），状态落独立帧文件。"""
    from bandi.skills import frame_store
    from bandi.skills.runtime import SkillFrameError, SkillRuntime

    if action not in _FRAME_ACTIONS:
        raise typer.BadParameter(f"action 必须是 {'/'.join(_FRAME_ACTIONS)} 之一，得到 {action!r}")
    resolved_frames_dir = frames_dir or _DEFAULT_FRAMES_DIR
    runtime = SkillRuntime(skills_dir=skills_dir or _DEFAULT_SKILLS_DIR)
    try:
        stack = frame_store.load_frames(scope, resolved_frames_dir)
        if stack is None:
            stack = runtime.new_stack(scope)
        if action == "start":
            if not skill:
                raise typer.BadParameter("action=start 需要 --skill")
            stack = runtime.start(
                stack,
                runtime.load_card(skill),
                task_id=task_id or None,
                slots=_parse_slots(slot),
            )
        elif action == "advance":
            active_skill = stack.active.skill_id if stack.active else skill
            if not active_skill:
                raise typer.BadParameter("action=advance 需要激活帧或 --skill")
            stack = runtime.advance(
                stack, runtime.load_card(active_skill), next_node_id=next_node or None
            )
        elif action == "suspend":
            stack = runtime.suspend(stack)
        elif action == "resume":
            if not task_id:
                raise typer.BadParameter("action=resume 需要 --task-id")
            stack = runtime.resume(stack, task_id)
        elif action == "complete":
            stack = runtime.complete(stack)
        if action != "status":
            frame_store.save_frames(stack, resolved_frames_dir)
    except SkillFrameError as exc:
        typer.echo(
            json.dumps({"scope": scope, "action": action, "error": str(exc)}, ensure_ascii=False)
        )
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(_stack_summary(stack), ensure_ascii=False))


@skills_app.command("branch")
def branch_cmd(
    owner: str = typer.Option(..., "--owner", help="分支归属键（Agent / Employee 标识）"),
    action: str = typer.Option(..., "--action", help=" / ".join(_BRANCH_ACTIONS)),
    skill: str = typer.Option("", "--skill", help="技能卡标识（action=update 时可省）"),
    card_file: str = typer.Option(
        "", "--card-file", help="改写后的技能卡 JSON 路径（action=update 必填）"
    ),
    version: str = typer.Option("", "--version", help="目标版本（action=rollback 必填）"),
    skills_dir: str = typer.Option("", "--skills-dir", help="技能库目录（默认 ~/.bandi/skills）"),
    branches_dir: str = typer.Option(
        "", "--branches-dir", help="分支目录（默认 ~/.bandi/skills/branches）"
    ),
) -> None:
    """技能分支版本化（fork/update/sync/promote/rollback/versions），状态落独立分支文件。"""
    from bandi.skills.branching import SkillBranchError, SkillBranching

    if action not in _BRANCH_ACTIONS:
        raise typer.BadParameter(f"action 必须是 {'/'.join(_BRANCH_ACTIONS)} 之一，得到 {action!r}")
    manager = SkillBranching(
        skills_dir=skills_dir or _DEFAULT_SKILLS_DIR,
        branches_dir=branches_dir or None,
    )
    try:
        if action == "fork":
            branch = manager.fork(owner, manager.load_global_card(_require_skill(skill, action)))
            payload = _branch_summary(branch)
        elif action == "update":
            if not card_file:
                raise typer.BadParameter("action=update 需要 --card-file")
            card = _load_card_file(card_file)
            branch = manager.update(owner, card.skill_id, card)
            payload = _branch_summary(branch)
        elif action == "sync":
            skill_id = _require_skill(skill, action)
            branch = manager.sync(owner, skill_id, manager.load_global_card(skill_id))
            payload = _branch_summary(branch)
        elif action == "promote":
            promoted, branch = manager.promote(owner, _require_skill(skill, action))
            payload = {**_branch_summary(branch), "promoted_version": promoted.version}
        elif action == "rollback":
            skill_id = _require_skill(skill, action)
            if not version:
                raise typer.BadParameter("action=rollback 需要 --version")
            payload = _branch_summary(manager.rollback(owner, skill_id, version))
        else:
            skill_id = _require_skill(skill, action)
            payload = {
                "owner_id": owner,
                "skill_id": skill_id,
                "versions": [item.version for item in manager.versions(owner, skill_id)],
            }
    except SkillBranchError as exc:
        typer.echo(
            json.dumps({"owner": owner, "action": action, "error": str(exc)}, ensure_ascii=False)
        )
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, ensure_ascii=False))


@skills_app.command("distill")
def distill_cmd(
    source_file: str = typer.Option("", "--source-file", help="原始流程文本文件路径（UTF-8）"),
    text: str = typer.Option("", "--text", help="原始流程文本（与 --source-file 二选一）"),
    title: str = typer.Option("", "--title", help="技能标题（缺省取文件名）"),
    domain: str = typer.Option("", "--domain", help="业务领域（可选）"),
    skills_dir: str = typer.Option("", "--skills-dir", help="技能库目录（默认 ~/.bandi/skills）"),
) -> None:
    """从自然语言流程蒸馏 SOP 技能卡并落盘（SkillDistiller 生产入口）。"""
    from bandi.agent.factory import build_provider, build_tool_registry
    from bandi.core.config import BandiConfig
    from bandi.skills.distiller import (
        MODEL_TOOL_DESCRIPTION_CHAR_LIMIT,
        SkillDistiller,
        SkillDistillRequest,
    )

    raw_content = text.strip()
    resolved_title = title.strip()
    if source_file:
        if raw_content:
            raise typer.BadParameter("--source-file 与 --text 只能二选一")
        try:
            raw_content = Path(source_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            typer.echo(
                json.dumps(
                    {"source_file": source_file, "error": f"无法读取: {exc}"},
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1) from exc
        if not resolved_title:
            resolved_title = Path(source_file).stem
    if not raw_content:
        raise typer.BadParameter("需要 --source-file 或 --text 提供原始流程文本")

    available_tools = [
        {
            "name": tool.name,
            "description": (tool.description or "")[:MODEL_TOOL_DESCRIPTION_CHAR_LIMIT],
            "input_schema": dict(tool.parameters or {}),
        }
        for tool in build_tool_registry().list_tools()
    ]
    distiller = SkillDistiller(
        build_provider(BandiConfig.load()), skills_dir=skills_dir or _DEFAULT_SKILLS_DIR
    )
    try:
        response = asyncio.run(
            distiller.distill(
                SkillDistillRequest(
                    title=resolved_title,
                    raw_content=raw_content,
                    business_domain=domain.strip() or None,
                    available_tools=available_tools,
                ),
                session_id=f"skill_distill:{uuid.uuid4().hex[:8]}",
            )
        )
    except Exception as exc:
        typer.echo(json.dumps({"title": resolved_title, "error": str(exc)}, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "skill_id": response.draft_skill.skill_id,
                "name": response.draft_skill.name,
                "version": response.draft_skill.version,
                "warnings": response.warnings,
                "tool_suggestions": [
                    {
                        "name": item.name,
                        "resolution_status": item.resolution_status,
                        "matched_tool_name": item.matched_tool_name,
                    }
                    for item in response.tool_suggestions
                ],
            },
            ensure_ascii=False,
        )
    )


@skills_app.command("import")
def import_cmd(
    path: str = typer.Argument(..., help="技能目录或 SKILL.md 路径"),
    team: str = typer.Option("", "--team", help="团队归属（空=全局技能）"),
) -> None:
    """导入 SKILL.md 技能到技能库（--team 带属导入，承诺入口）。"""
    from bandi.skills.importer import SkillImporter, SkillImportError

    try:
        result = SkillImporter().import_skill(path, team_id=team)
    except SkillImportError as exc:
        typer.echo(json.dumps({"imported": False, "error": str(exc)}, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "imported": True,
                "skill": result.skill_name,
                "path": result.skill_path,
                "team_id": team,
            },
            ensure_ascii=False,
        )
    )


def _require_skill(skill: str, action: str) -> str:
    """功能：校验 --skill 必填（缺失即提示用法，不静默取空）。"""
    if not skill:
        raise typer.BadParameter(f"action={action} 需要 --skill")
    return skill


def _load_card_file(path: str) -> Any:
    """功能：从 JSON 文件读入技能卡（供 branch update 使用）。"""
    from bandi.skills.card import SkillCard

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"无法读取技能卡文件: {path}: {exc}") from exc
    try:
        return SkillCard(**data)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"技能卡内容非法: {path}: {exc}") from exc


def _branch_summary(branch: SkillBranch) -> dict[str, Any]:
    """功能：分支可读摘要（复用分支模型，不复制字段语义）。"""
    return {
        "owner_id": branch.owner_id,
        "skill_id": branch.skill_id,
        "base_version": branch.base_version,
        "head_version": branch.head_version,
        "sync_state": branch.sync_state.value,
        "snapshot_count": len(branch.versions),
    }


def _parse_slots(items: list[str] | None) -> dict[str, Any]:
    """功能：把 --slot k=v 列表解析为槽位字典（缺 = 号的项直接报错，不静默丢弃）。"""
    slots: dict[str, Any] = {}
    for item in items or []:
        key, separator, value = str(item).partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter(f"--slot 需要 k=v 形式，得到 {item!r}")
        slots[key.strip()] = value.strip()
    return slots


def _stack_summary(stack: SkillFrameStack) -> dict[str, Any]:
    """功能：帧栈可读摘要（复用帧模型，不复制字段语义）。"""
    return {
        "scope_id": stack.scope_id,
        "active": _frame_brief(stack.active),
        "pending": [_frame_brief(frame) for frame in stack.pending],
        "completed_count": len(stack.completed),
    }


def _frame_brief(frame: SkillFrame | None) -> dict[str, Any] | None:
    if frame is None:
        return None
    return {
        "task_id": frame.task_id,
        "skill_id": frame.skill_id,
        "node_id": frame.node_id,
        "status": frame.status.value,
        "awaiting_fields": frame.awaiting_fields,
    }
