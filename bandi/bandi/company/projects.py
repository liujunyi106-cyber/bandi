# 功能：项目归属记录（~/.bandi/company/projects/<pid>.json）——ProjectRecord + ProjectStore
# （CRUD / 原子写 / 路径安全）+ 团队拉入校验（已组建口径）+ 成员动态解析（
# 共享跨项目引用，员工不复制；org 变更即时反映，不落盘快照）。
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bandi.company.team_template import TeamTemplateError
from bandi.core.exceptions import BandiError
from bandi.skills.path_guard import validate_path_segment

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR = "~/.bandi/company/projects"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectError(BandiError):
    """项目操作失败（参数非法 / 落盘读写失败）。"""


class ProjectNotFoundError(ProjectError):
    """项目不存在。"""


class ProjectExistsError(ProjectError):
    """项目已存在（创建入口拒绝覆盖）。"""


class ProjectSaveError(ProjectError):
    """项目写盘失败。"""


class ProjectLoadError(ProjectError):
    """项目读取失败：文件非法 JSON / schema 校验不符（与缺失区分）。"""


class ProjectTeamNotFoundError(ProjectError):
    """团队完全不存在（无已组建角色且模板不可见）。"""


class ProjectTeamNotReadyError(ProjectError):
    """团队尚未组建：模板存在但 org 中无该 team_id 归属角色（请先实例化）。"""


def _validate_project_id(project_id: str) -> str:
    """功能：项目标识路径安全校验（store 入口双保险，防 model_construct 绕过模型校验）。"""
    return validate_path_segment(str(project_id or ""), label="project_id", error_cls=ProjectError)


AGENTS_MD_FILENAME = "AGENTS.md"


def ensure_agents_md(root_dir: str) -> bool:
    """功能：项目首次发言时确保工作目录存在空 AGENTS.md（opencode/claude code 语义）。

    已存在（无论内容）严格不覆盖；目录缺失/非法/写失败返回 False（不阻断对话）。
    返回 True 表示本次创建了新文件。
    """
    root = str(root_dir or "").strip()
    if not root:
        return False
    base = Path(root).expanduser()
    if not base.is_dir():
        return False
    path = base / AGENTS_MD_FILENAME
    if path.exists():
        return False
    try:
        path.write_text("", encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("创建 AGENTS.md 失败: %s: %s", path, exc)
        return False
    return True


# 功能：项目文件列表
_FILE_LIST_MAX = 2000
_IGNORED_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}
)


def list_project_files(root_dir: str, query: str = "", limit: int = _FILE_LIST_MAX) -> list[dict]:
    """功能：项目文件遍历（忽略目录 + 上限；仅文件；root 未设置/不存在 → 空列表）。"""
    base_raw = str(root_dir or "").strip()
    if not base_raw:
        return []
    base = Path(base_raw).expanduser()
    if not base.is_dir():
        return []
    q = str(query or "").strip().lower()
    files: list[dict] = []
    stack = [base]
    while stack and len(files) < limit:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if len(files) >= limit:
                break
            try:
                if child.is_dir():
                    if child.name not in _IGNORED_DIRS:
                        stack.append(child)
                    continue
            except OSError:
                continue
            rel = child.relative_to(base).as_posix()
            if q and q not in rel.lower():
                continue
            files.append({"path": rel, "name": child.name, "is_dir": False})
    return files


class ProjectRecord(BaseModel):
    """功能：项目归属记录——项目标识 / 显示名 / 工作目录 / 拉入团队引用（动态解析成员）。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str  # 功能：项目标识（= 库文件名，路径安全校验）
    name: str  # 功能：项目显示名（非空）
    root_dir: str = ""  # 功能：项目工作目录（顶栏项目·工作目录选择器消费）
    team_ids: list[str] = Field(default_factory=list)  # 功能：拉入团队引用（去重保序）
    dissolved_team_ids: list[str] = Field(default_factory=list)  # 功能：已解散团队标记
    created_at: str = ""  # 功能：首次落盘时刻（store 维护）
    updated_at: str = ""  # 功能：最近落盘时刻（store 维护）

    @field_validator("project_id")
    @classmethod
    def _check_project_id(cls, value: str) -> str:
        try:
            return _validate_project_id(value)
        except ProjectError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("项目 name 不能为空")
        return value

    @field_validator("team_ids", "dissolved_team_ids")
    @classmethod
    def _check_team_ids(cls, value: list[str]) -> list[str]:
        unique: list[str] = []
        for item in value:
            try:
                validated = validate_path_segment(
                    str(item or ""), label="team_id", error_cls=ProjectError
                )
            except ProjectError as exc:
                raise ValueError(str(exc)) from exc
            if validated not in unique:
                unique.append(validated)
        return unique


@dataclass(frozen=True)
class ProjectMember:
    """功能：项目成员（团队角色 + 1:1 员工）——resolve_members 动态解析结果，不落盘。"""

    team_id: str
    role_id: str
    role_name: str
    employee_id: str = ""
    employee_name: str = ""
    employee_status: str = ""


class ProjectStore:
    """功能：项目归属记录本地库——CRUD（原子写 + 时间戳）+ 团队拉入 + 成员动态解析。"""

    def __init__(self, projects_dir: str = DEFAULT_PROJECTS_DIR) -> None:
        self.projects_dir = projects_dir

    def _base_dir(self) -> Path:
        return Path(self.projects_dir).expanduser()

    def project_path(self, project_id: str) -> Path:
        """功能：项目文件路径（project_id 越界显式失败，不触碰目录外）。"""
        return self._base_dir() / f"{_validate_project_id(project_id)}.json"

    # ── CRUD ────────────────────────────────────────────────────────────────

    def save_project(self, record: ProjectRecord) -> ProjectRecord:
        """功能：原子写项目到库；刷新 updated_at（首次落盘同时填 created_at）。

        save 入口 re-validate（防 model_copy/model_construct 绕过校验写出坏记录）；
        replace 瞬态失败（Windows 并发覆盖偶发 OSError）重试 2 次。
        """
        try:
            record = ProjectRecord.model_validate(record.model_dump())
        except ValueError as exc:
            raise ProjectSaveError(f"项目校验失败（save 入口 re-validate）: {exc}") from exc
        path = self.project_path(record.project_id)
        now = _now()
        saved = record.model_copy(
            update={"created_at": record.created_at or now, "updated_at": now}
        )
        text = json.dumps(saved.model_dump(), ensure_ascii=False, indent=2)
        tmp = path.parent / f".{saved.project_id}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            tmp.unlink(missing_ok=True)
            raise ProjectSaveError(f"项目写盘失败: {path}: {exc}") from exc
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
            raise ProjectSaveError(f"项目写盘失败: {path}: {last_error}") from last_error
        return saved

    def create_project(self, project_id: str, name: str, root_dir: str = "") -> ProjectRecord:
        """功能：创建项目；已存在默认拒绝（fail-closed），参数非法显式失败。"""
        try:
            record = ProjectRecord(project_id=project_id, name=name, root_dir=root_dir)
        except ValueError as exc:
            raise ProjectError(f"项目参数非法: {exc}") from exc
        if self.project_path(record.project_id).is_file():
            raise ProjectExistsError(f"项目已存在（拒绝覆盖）: {record.project_id}")
        return self.save_project(record)

    def get_project(self, project_id: str) -> ProjectRecord | None:
        """功能：读项目——缺失返回 None；坏文件显式失败（与缺失区分）。"""
        path = self.project_path(project_id)
        if not path.is_file():
            return None
        return self._load_file(path)

    def list_projects(self) -> list[ProjectRecord]:
        """功能：全量项目（按 project_id 排序）；单个坏文件告警跳过，不 brick 整库列表。"""
        base = self._base_dir()
        if not base.is_dir():
            return []
        records: list[ProjectRecord] = []
        for entry in sorted(base.glob("*.json")):
            try:
                _validate_project_id(entry.stem)
            except ProjectError:
                continue
            try:
                records.append(self._load_file(entry))
            except ProjectLoadError as exc:
                logger.warning("跳过坏项目文件 %s: %s", entry, exc)
        return records

    def rename_project(self, project_id: str, name: str) -> ProjectRecord:
        """功能：改项目名——仅改 name 并推进 updated_at；缺失 / 空名显式失败。

        project_id 不可通过本方法变更（路径为准）；项目名不要求唯一。
        """
        cleaned = (name or "").strip()
        if not cleaned:
            raise ProjectError("项目名不能为空")
        record = self.get_project(project_id)
        if record is None:
            raise ProjectNotFoundError(f"项目不存在: {project_id}")
        return self.save_project(record.model_copy(update={"name": cleaned}))

    def delete_project(self, project_id: str) -> bool:
        """功能：删除项目（True）；缺失返回 False。"""
        path = self.project_path(project_id)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise ProjectError(f"项目删除失败: {path}: {exc}") from exc
        return True

    # ── 团队拉入与成员解析（共享跨项目引用）──────────────────────────

    def add_team(
        self,
        project_id: str,
        team_id: str,
        org_engine: Any,
        *,
        template_store: Any | None = None,
    ) -> ProjectRecord:
        """功能：拉入团队（已组建校验）——重复拉入幂等返回；未组建 NotReady / 不存在 NotFound。

        校验口径（=A）：org 中存在 `team_id` 归属角色 = 已组建；
        模板存在但未实例化 → 400 语义（ProjectTeamNotReadyError）；完全不存在 → 404 语义。
        """
        record = self.get_project(project_id)
        if record is None:
            raise ProjectNotFoundError(f"项目不存在: {project_id}")
        validated = validate_path_segment(team_id, label="team_id", error_cls=ProjectError)
        if not self._team_roles(validated, org_engine):
            if self._template_exists(template_store, validated):
                raise ProjectTeamNotReadyError(f"团队尚未组建（请先实例化）: {validated}")
            raise ProjectTeamNotFoundError(f"团队不存在: {validated}")
        dissolved = [item for item in record.dissolved_team_ids if item != validated]
        if validated in record.team_ids and dissolved == record.dissolved_team_ids:
            return record
        updated = record.model_copy(
            update={
                "team_ids": [*record.team_ids, validated]
                if validated not in record.team_ids
                else record.team_ids,
                "dissolved_team_ids": dissolved,
            }
        )
        return self.save_project(updated)

    def resolve_members(self, project_id: str, org_engine: Any) -> list[ProjectMember]:
        """功能：成员动态解析——按 team_ids 顺序 × 角色注册序，角色 1:1 关联员工。

        不落盘快照（=A）：org 增删角色/员工即时反映。
        """
        record = self.get_project(project_id)
        if record is None:
            raise ProjectNotFoundError(f"项目不存在: {project_id}")
        agents = list(org_engine.list_agents())
        employees = list(org_engine.list_employees(include_inactive=True))
        by_role = {str(employee.role_id): employee for employee in employees}
        members: list[ProjectMember] = []
        for team_id in record.team_ids:
            for role in agents:
                if str(getattr(role, "team_id", "") or "") != team_id:
                    continue
                employee = by_role.get(str(role.id))
                members.append(
                    ProjectMember(
                        team_id=team_id,
                        role_id=str(role.id),
                        role_name=str(role.name),
                        employee_id=str(employee.employee_id) if employee else "",
                        employee_name=str(employee.name) if employee else "",
                        employee_status=str(employee.status) if employee else "",
                    )
                )
        return members

    @staticmethod
    def _team_roles(team_id: str, org_engine: Any) -> list[Any]:
        return [
            role
            for role in org_engine.list_agents()
            if str(getattr(role, "team_id", "") or "") == team_id
        ]

    @staticmethod
    def _template_exists(template_store: Any | None, team_id: str) -> bool:
        if template_store is None:
            return False
        try:
            return template_store.get_template(team_id) is not None
        except TeamTemplateError:
            return False

    # ── 内部 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_file(path: Path) -> ProjectRecord:
        """功能：读文件构造项目——非法 JSON / 非 UTF-8 / schema 不符归 LoadError（与缺失区分）。"""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectLoadError(f"项目文件读取失败: {path}: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProjectLoadError(f"项目文件非法 JSON: {path}: {exc}") from exc
        try:
            return ProjectRecord(**payload)
        except (TypeError, ValueError) as exc:
            raise ProjectLoadError(f"项目文件非法（schema 校验失败）: {path}: {exc}") from exc
