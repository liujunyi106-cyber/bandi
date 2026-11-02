# 功能：团队模板本地库（~/.bandi/teams/<template_id>.json）——CRUD + 导出/导入 +
# 原子写 + 路径安全；读取时合并内置模板（落盘同名覆盖内置，内置不可删）。
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.company.agent_profile import AgentResourceDuplicateError
from bandi.company.org_engine import OrgEmployee
from bandi.company.team_template import (
    TeamTemplate,
    TeamTemplateError,
    TeamTemplateLoadError,
    TeamTemplateNotFoundError,
    TeamTemplateSaveError,
    builtin_templates,
)
from bandi.skills.path_guard import validate_path_segment

logger = logging.getLogger(__name__)

DEFAULT_TEAMS_DIR = "~/.bandi/teams"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_template_id(template_id: str) -> str:
    """功能：模板标识路径安全校验（store 入口双保险，防 model_construct 绕过模型校验）。"""
    return validate_path_segment(
        str(template_id or ""), label="template_id", error_cls=TeamTemplateError
    )


class TeamTemplateStore:
    """功能：团队模板本地库——落盘模板与内置模板合并读取，原子写。"""

    def __init__(self, templates_dir: str = DEFAULT_TEAMS_DIR) -> None:
        self.templates_dir = templates_dir

    def _base_dir(self) -> Path:
        return Path(self.templates_dir).expanduser()

    def template_path(self, template_id: str) -> Path:
        """功能：模板文件路径（template_id 越界显式失败，不触碰目录外）。"""
        return self._base_dir() / f"{_validate_template_id(template_id)}.json"

    # ── CRUD ────────────────────────────────────────────────────────────────

    def save_template(self, template: TeamTemplate) -> TeamTemplate:
        """功能：原子写模板到库；刷新 updated_at（首次落盘同时填 created_at）。

        save 入口 re-validate（防 model_copy/model_construct 绕过校验写出坏模板）；
        replace 瞬态失败（Windows 并发覆盖偶发 OSError）重试 2 次。
        """
        try:
            template = TeamTemplate.model_validate(template.model_dump())
        except ValueError as exc:
            raise TeamTemplateSaveError(f"模板校验失败（save 入口 re-validate）: {exc}") from exc
        path = self.template_path(template.template_id)
        now = _now()
        saved = template.model_copy(
            update={"created_at": template.created_at or now, "updated_at": now}
        )
        text = json.dumps(saved.model_dump(), ensure_ascii=False, indent=2)
        tmp = path.parent / f".{template.template_id}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            tmp.unlink(missing_ok=True)
            raise TeamTemplateSaveError(f"模板写盘失败: {path}: {exc}") from exc
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                tmp.replace(path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05)
        if last_error is not None:
            tmp.unlink(missing_ok=True)
            raise TeamTemplateSaveError(f"模板写盘失败: {path}: {last_error}") from last_error
        return saved

    def get_template(self, template_id: str) -> TeamTemplate | None:
        """功能：读模板——落盘优先，缺失回落内置；坏文件显式失败（与缺失区分）。"""
        path = self.template_path(template_id)
        if path.is_file():
            return self._load_file(path)
        for builtin in builtin_templates():
            if builtin.template_id == template_id:
                return builtin
        return None

    def list_templates(self) -> list[TeamTemplate]:
        """功能：全量模板——内置打底、落盘同名覆盖；按 template_id 排序。

        单个坏文件（坏 JSON/非 UTF-8/结构非法）告警跳过，不 brick 整库列表
        （get/import 保持严格语义，仍显式失败）。
        """
        merged: dict[str, TeamTemplate] = {
            builtin.template_id: builtin for builtin in builtin_templates()
        }
        base = self._base_dir()
        if base.is_dir():
            for entry in sorted(base.glob("*.json")):
                try:
                    _validate_template_id(entry.stem)
                except TeamTemplateError:
                    continue
                try:
                    merged[entry.stem] = self._load_file(entry)
                except TeamTemplateLoadError as exc:
                    logger.warning("跳过坏模板文件 %s: %s", entry, exc)
        return [merged[template_id] for template_id in sorted(merged)]

    def delete_template(self, template_id: str) -> bool:
        """功能：删除落盘模板（True）；缺失或内置（不可删）返回 False。"""
        path = self.template_path(template_id)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise TeamTemplateError(f"模板删除失败: {path}: {exc}") from exc
        return True

    # ── 导出 / 导入 ─────────────────────────────────────────────────────────

    def export_template(self, template_id: str, out_path: str | Path) -> Path:
        """功能：导出模板（落盘或内置）为 JSON 文件到调用方指定路径（原子写）。"""
        template = self.get_template(template_id)
        if template is None:
            raise TeamTemplateNotFoundError(f"模板不存在: {template_id}")
        target = Path(out_path)
        text = json.dumps(template.model_dump(), ensure_ascii=False, indent=2)
        tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
        except (OSError, ValueError, TypeError) as exc:
            tmp.unlink(missing_ok=True)
            raise TeamTemplateSaveError(f"模板导出失败: {target}: {exc}") from exc
        return target

    def import_template(self, path: str | Path, overwrite: bool = False) -> TeamTemplate:
        """功能：导入 JSON 模板入库；落盘已存在默认拒绝（fail-closed），overwrite=True 覆盖。"""
        source = Path(path)
        template = self._load_file(source)
        on_disk = self.template_path(template.template_id).is_file()
        if on_disk and not overwrite:
            raise TeamTemplateError(
                f"模板已存在（overwrite=False 拒绝覆盖）: {template.template_id}"
            )
        return self.save_template(template)

    # ── 内部 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_file(path: Path) -> TeamTemplate:
        """功能：读文件构造模板——非法 JSON / 非 UTF-8 / schema 不符归 LoadError（与缺失区分）。"""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TeamTemplateLoadError(f"模板文件读取失败: {path}: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TeamTemplateLoadError(f"模板文件非法 JSON: {path}: {exc}") from exc
        try:
            return TeamTemplate(**payload)
        except (TypeError, ValueError) as exc:
            raise TeamTemplateLoadError(f"模板文件非法（schema 校验失败）: {path}: {exc}") from exc


@dataclass
class TeamInstantiationReport:
    """功能：实例化报告——角色/员工/档案/席位的新建与跳过清单 + best-effort 警告。"""

    team_id: str
    prefix: str
    role_ids: list[str] = field(default_factory=list)
    created_role_ids: list[str] = field(default_factory=list)
    skipped_role_ids: list[str] = field(default_factory=list)
    employee_ids: list[str] = field(default_factory=list)
    created_employee_ids: list[str] = field(default_factory=list)
    skipped_employee_ids: list[str] = field(default_factory=list)
    profile_ids: list[str] = field(default_factory=list)
    seat_role_ids: list[str] = field(default_factory=list)
    work_item_ids: list[str] = field(default_factory=list)
    created_work_item_ids: list[str] = field(default_factory=list)
    skipped_work_item_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_org_from_template(
    template: TeamTemplate,
    org_engine: Any,
    *,
    recruiter: Any | None = None,
    profile_manager: Any | None = None,
) -> TeamInstantiationReport:
    """功能：一键实例化——角色（namespace 前缀）/ 员工 1:1 / 席位 / profiles 装配。

    =A+：角色 id 统一 `<team_id>_<role_id>` 前缀（下划线分隔——全链路
    路径安全：角色/员工/profile/session 共享同一 id 口径；幂等重入跳过不覆盖
    reports_to 收敛回模板声明）；=A：每角色派生 1 员工（含 llm_config）。
    recruiter / profile_manager 缺省时跳过对应装配；profiles 技能绑定 best-effort
    （重复绑定跳过、缺失/非法进 warnings 不中断实例化）。
    参考先例：package_loader.install（两遍注册）/ org_persistence.load_org（坏数据降级）。
    """
    team_id = str(template.team_id or "").strip() or template.template_id
    team_id = validate_path_segment(team_id, label="team_id", error_cls=TeamTemplateError)
    prefix = f"{team_id}_"
    report = TeamInstantiationReport(team_id=team_id, prefix=prefix)

    from bandi.company.employee_lifecycle import EmployeeTombstoneStore

    tombstones = EmployeeTombstoneStore()
    pending_reports: dict[str, str | None] = {}
    for role in template.roles:
        new_id = f"{prefix}{role.id}"
        if tombstones.get(new_id) is not None:
            raise TeamTemplateError(f"IDENTITY_RESERVED: {new_id}")
        report.role_ids.append(new_id)
        existing_role = org_engine.get_role(new_id)
        if existing_role is None:
            org_engine.register_role(
                new_id,
                role.name,
                role.responsibility,
                None,  # reports_to 第二遍补（引用可能尚未注册）
                [f"{prefix}{spawn_id}" for spawn_id in role.can_spawn],
                {},
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id=team_id,
                fs_scopes=list(role.fs_scopes),
            )
            report.created_role_ids.append(new_id)
        else:
            report.skipped_role_ids.append(new_id)
            existing_team = str(getattr(existing_role, "team_id", "") or "")
            if existing_team != team_id:
                report.warnings.append(
                    f"角色 {new_id} 已存在但归属团队为 `{existing_team}`"
                    f"（期望 `{team_id}`），已跳过（不覆盖）"
                )
        pending_reports[new_id] = f"{prefix}{role.reports_to}" if role.reports_to else None
    for new_id, reports_to in pending_reports.items():
        if not reports_to:
            continue
        try:
            org_engine.set_reports_to(new_id, reports_to)
        except (ValueError, AttributeError) as exc:
            report.warnings.append(f"reports_to 设置失败 {new_id} -> {reports_to}: {exc}")

    for role in template.roles:
        new_id = f"{prefix}{role.id}"
        report.employee_ids.append(new_id)
        existing_employee = org_engine.get_employee(new_id)
        if existing_employee is not None:
            report.skipped_employee_ids.append(new_id)
            existing_role_id = str(getattr(existing_employee, "role_id", "") or "")
            if existing_role_id != new_id:
                report.warnings.append(
                    f"员工 {new_id} 已存在但绑定角色为 `{existing_role_id}`"
                    f"（期望 `{new_id}`），已跳过（不覆盖）"
                )
            continue
        occupant = next(
            (
                item
                for item in org_engine.list_employees(include_inactive=True)
                if str(getattr(item, "role_id", "") or "") == new_id
                and str(getattr(item, "status", "active")) in {"active", "paused"}
            ),
            None,
        )
        if occupant is not None:
            raise TeamTemplateError(f"ROLE_ALREADY_STAFFED: {new_id} -> {occupant.employee_id}")
        org_engine.register_employee(
            OrgEmployee(
                employee_id=new_id,
                name=role.name,
                role_id=new_id,
                description=role.responsibility,
                template_id=template.template_id,
                metadata={"team_id": team_id, "source": "team_template"},
                llm_config=dict(role.llm_config) or None,
            )
        )
        report.created_employee_ids.append(new_id)

    if recruiter is not None:
        for role_id in report.role_ids:
            if recruiter.get_seat(role_id) is None:
                recruiter.register_seat(role_id)
                report.seat_role_ids.append(role_id)
            recruiter.recruit(role_id)

    if profile_manager is not None:
        for role in template.roles:
            profile_id = f"{prefix}{role.id}"
            try:
                if profile_manager.get_profile(profile_id) is None:
                    profile_manager.create_profile(
                        profile_id,
                        name=role.name,
                        description=role.responsibility,
                        metadata={"team_id": team_id, "template_id": template.template_id},
                    )
                    report.profile_ids.append(profile_id)
            except Exception as exc:  # noqa: BLE001 — best-effort：建档失败进 warnings 不中断
                report.warnings.append(f"档案建档失败 {profile_id}: {exc}")
                continue
            for skill_ref in role.skill_refs:
                try:
                    profile_manager.bind_resource(profile_id, "general_skill", skill_ref)
                except AgentResourceDuplicateError:
                    continue
                except Exception as exc:  # noqa: BLE001 — best-effort：缺失技能进 warnings
                    report.warnings.append(f"技能绑定失败 {profile_id}/{skill_ref}: {exc}")
    return report
