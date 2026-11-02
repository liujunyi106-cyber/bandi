# 功能：技能分支版本化（per-owner 分支：fork / 更新 / 同步 / 晋升 / 回滚 + semver 快照）。
# AgentSkillBranchVersion + `ensure_agent_skill_branch`（fork）/`update_branch_skill`/
# `sync_branch_from_overall`/`promote_branch_to_overall`/`rollback_branch` + next_*_version。
# per-Employee 均可承载）；② 分支落**独立文件**（复用 serializer 原子写先例）；
# 不改 `EmployeeEvolutionManager`（其 SKILL.md + 整数版本口径与本块互不干扰）。
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard
from bandi.skills.path_guard import validate_path_segment
from bandi.skills.serializer import SkillCardLoadError, load_card, save_card

_BRANCH_SUFFIX = ".branch.json"
_TEMP_PREFIX = ".{skill_id}.tmp-{pid}-{token}"
MAX_BRANCH_SNAPSHOTS = 200
DEFAULT_SKILLS_DIR = "~/.bandi/skills"
DEFAULT_BRANCHES_DIR = "~/.bandi/skills/branches"


class SkillBranchError(BandiError):
    """分支操作失败（参数非法 / 文件读写失败）。"""


class SkillBranchNotFoundError(SkillBranchError):
    """分支或分支版本不存在。"""


class SkillBranchMismatchError(SkillBranchError):
    """传入卡片的 skill_id 与目标分支不一致。"""


class SkillBranchConflictError(SkillBranchError):
    """整体技能卡基线已偏离分支记录（他人已改动），拒绝覆盖式晋升。"""


class SkillBranchSyncState(StrEnum):
    SYNCED = "synced"  # 功能：分支内容与整体基线一致
    DIVERGED = "diverged"  # 功能：分支存在本地改写（基线之后有变更）


class SkillBranchVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str  # 功能：该快照的三段式 semver
    card: SkillCard  # 功能：快照时刻的完整技能卡
    change_summary: str = ""  # 功能：本次变更说明（初始化/改写/同步/推送）
    created_at: str  # 功能：快照时刻（ISO 8601）


class SkillBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str  # 功能：分支归属键（调用方语义：Agent / Employee 等）
    skill_id: str  # 功能：被分支的技能卡标识
    base_version: str  # 功能：分支基线版本（最近一次同步/晋升来源的整体版本）
    head_version: str  # 功能：分支当前版本
    card: SkillCard  # 功能：分支当前内容
    sync_state: SkillBranchSyncState  # 功能：与整体基线的偏离状态
    created_at: str  # 功能：分支创建时刻
    updated_at: str  # 功能：分支最近更新时刻
    versions: list[SkillBranchVersion] = Field(default_factory=list)  # 功能：版本快照历史
    revision: int = 0  # 功能：落盘版本号（并发写乐观锁；load 读回、save 比对并递增）


class SkillBranching:
    """功能：技能分支管理器——fork 全局技能到私有分支，支持改写、同步、晋升与回滚。"""

    def __init__(
        self,
        *,
        skills_dir: str = DEFAULT_SKILLS_DIR,
        branches_dir: str | None = None,
    ) -> None:
        self.skills_dir = skills_dir
        self.branches_dir = branches_dir or DEFAULT_BRANCHES_DIR

    def branch_path(self, owner_id: str, skill_id: str) -> Path:
        """功能：分支文件路径（owner_id / skill_id 均做路径安全校验）。"""
        _validate_segment(owner_id, "owner_id", SkillBranchError)
        _validate_segment(skill_id, "skill_id", SkillBranchError)
        return Path(self.branches_dir).expanduser() / owner_id / f"{skill_id}{_BRANCH_SUFFIX}"

    def load_branch(self, owner_id: str, skill_id: str) -> SkillBranch | None:
        """功能：读回分支；文件不存在返回 None，内容非法显式失败。"""
        path = self.branch_path(owner_id, skill_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SkillBranchError(f"无法读取分支: {path}: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SkillBranchError(f"分支文件非法 JSON: {path}: {exc}") from exc
        try:
            return SkillBranch(**payload)
        except (TypeError, ValueError) as exc:
            raise SkillBranchError(f"分支 Schema 校验失败: {path}: {exc}") from exc

    def save_branch(self, branch: SkillBranch) -> Path:
        """功能：分支原子落盘到 branches_dir/<owner_id>/<skill_id>.branch.json。
        并发写乐观锁：磁盘 revision 与本次 branch.revision 不一致时显式
        拒绝（不得静默覆盖他人改动）；保存成功回写 branch.revision = 磁盘值 + 1。"""
        final_path = self.branch_path(branch.owner_id, branch.skill_id)
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            raise SkillBranchError(f"无法创建分支目录: {final_path.parent}: {exc}") from exc
        disk_revision = _read_disk_revision(final_path)
        if disk_revision is not None and disk_revision != branch.revision:
            raise SkillBranchConflictError(
                f"分支文件已被其他进程修改（磁盘 revision={disk_revision}，"
                f"本次基于 revision={branch.revision}）：请重新加载后再保存: {final_path}"
            )
        branch.revision = (disk_revision if disk_revision is not None else branch.revision) + 1
        tmp_path = final_path.parent / _TEMP_PREFIX.format(
            skill_id=branch.skill_id, pid=os.getpid(), token=uuid.uuid4().hex[:8]
        )
        try:
            data = json.dumps(branch.model_dump(mode="json"), ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise SkillBranchError(f"分支含不可 JSON 序列化字段: {exc}") from exc
        try:
            tmp_path.write_text(data, encoding="utf-8")
            tmp_path.replace(final_path)
        except (OSError, ValueError) as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SkillBranchError(f"分支写入失败: {final_path}: {exc}") from exc
        return final_path

    def fork(self, owner_id: str, card: SkillCard) -> SkillBranch:
        """功能：把全局卡 fork 成 owner 私有分支（同 owner+skill 幂等，已存在即原样返回）。"""
        existing = self.load_branch(owner_id, card.skill_id)
        if existing is not None:
            return existing
        now = _now()
        branch = SkillBranch(
            owner_id=owner_id,
            skill_id=card.skill_id,
            base_version=card.version,
            head_version=card.version,
            card=card,
            sync_state=SkillBranchSyncState.SYNCED,
            created_at=now,
            updated_at=now,
            versions=[
                SkillBranchVersion(
                    version=card.version,
                    card=card,
                    change_summary="初始化分支",
                    created_at=now,
                )
            ],
        )
        self.save_branch(branch)
        return branch

    def update(
        self,
        owner_id: str,
        skill_id: str,
        card: SkillCard,
        *,
        change_summary: str = "分支改写",
    ) -> SkillBranch:
        """功能：改写分支内容——版本 minor 递增 + 快照追加 + 标记 diverged。"""
        branch = self._require_branch(owner_id, skill_id)
        self._require_same_skill(card, skill_id)
        next_version = self._next_unique_branch_version(branch)
        updated_card = card.model_copy(update={"version": next_version})
        now = _now()
        return self._persist(
            branch.model_copy(
                update={
                    "card": updated_card,
                    "head_version": next_version,
                    "sync_state": SkillBranchSyncState.DIVERGED,
                    "updated_at": now,
                    "versions": self._append_version(
                        branch, next_version, updated_card, change_summary
                    ),
                }
            )
        )

    def sync(self, owner_id: str, skill_id: str, global_card: SkillCard) -> SkillBranch:
        """功能：从整体基线同步——本地改写被覆盖，base/head 对齐整体版本并回 synced。"""
        branch = self._require_branch(owner_id, skill_id)
        self._require_same_skill(global_card, skill_id)
        now = _now()
        return self._persist(
            branch.model_copy(
                update={
                    "card": global_card,
                    "base_version": global_card.version,
                    "head_version": global_card.version,
                    "sync_state": SkillBranchSyncState.SYNCED,
                    "updated_at": now,
                    "versions": self._append_version(
                        branch, global_card.version, global_card, "同步整体版本"
                    ),
                }
            )
        )

    def promote(self, owner_id: str, skill_id: str) -> tuple[SkillCard, SkillBranch]:
        """功能：把分支内容晋升到整体技能库——整体版本 minor 递增并落盘，分支回 synced。
        **基线冲突检测**：整体卡版本已偏离本分支 `base_version` 时拒绝晋升
        （说明他人在本分支 fork/同步之后改过整体卡，直接覆盖会丢其改动）。"""
        branch = self._require_branch(owner_id, skill_id)
        global_card = self.load_global_card(skill_id)
        if global_card.version != branch.base_version:
            raise SkillBranchConflictError(
                f"整体卡版本已变更（base={branch.base_version} -> 当前={global_card.version}），"
                "拒绝覆盖他人改动；请先 sync 对齐后再 promote。"
            )
        next_version = next_global_version(global_card.version)
        promoted = branch.card.model_copy(update={"version": next_version})
        # 先完成快照上限校验（可能抛错）再写全局卡——保证 promote 原子：
        # 失败时全局卡与分支均逐字不变（不得留下已提升未同步的中间态）。
        updated_versions = self._append_version(branch, next_version, promoted, "推送到整体")
        save_card(promoted, self.skills_dir)
        now = _now()
        synced = self._persist(
            branch.model_copy(
                update={
                    "card": promoted,
                    "base_version": next_version,
                    "head_version": next_version,
                    "sync_state": SkillBranchSyncState.SYNCED,
                    "updated_at": now,
                    "versions": updated_versions,
                }
            )
        )
        return promoted, synced

    def rollback(self, owner_id: str, skill_id: str, version: str) -> SkillBranch:
        """功能：回滚分支到指定快照版本（快照历史不裁剪；sync_state 依基线判定）。"""
        branch = self._require_branch(owner_id, skill_id)
        snapshot = next((item for item in branch.versions if item.version == version), None)
        if snapshot is None:
            raise SkillBranchNotFoundError(f"分支版本不存在: {skill_id}@{version}")
        return self._persist(
            branch.model_copy(
                update={
                    "card": snapshot.card,
                    "head_version": snapshot.version,
                    "sync_state": (
                        SkillBranchSyncState.SYNCED
                        if snapshot.version == branch.base_version
                        else SkillBranchSyncState.DIVERGED
                    ),
                    "updated_at": _now(),
                }
            )
        )

    def versions(self, owner_id: str, skill_id: str) -> list[SkillBranchVersion]:
        """功能：分支版本快照列表（按时间顺序，同文件内记录顺序）。"""
        return list(self._require_branch(owner_id, skill_id).versions)

    @staticmethod
    def _append_version(
        branch: SkillBranch,
        version: str,
        card: SkillCard,
        change_summary: str,
    ) -> list[SkillBranchVersion]:
        """功能：追加版本快照——同版本已存在则原样返回（幂等，对齐参考源
        `_ensure_branch_version`：同一版本不重复落快照）。"""
        if any(item.version == version for item in branch.versions):
            return list(branch.versions)
        if len(branch.versions) >= MAX_BRANCH_SNAPSHOTS:
            raise SkillBranchError(
                f"分支快照已达上限 {MAX_BRANCH_SNAPSHOTS}（fail-closed，不丢弃历史）："
                "请归档旧快照或另建分支后继续。"
            )
        return [
            *branch.versions,
            SkillBranchVersion(
                version=version,
                card=card,
                change_summary=change_summary,
                created_at=_now(),
            ),
        ]

    def _persist(self, branch: SkillBranch) -> SkillBranch:
        self.save_branch(branch)
        return branch

    def _require_branch(self, owner_id: str, skill_id: str) -> SkillBranch:
        branch = self.load_branch(owner_id, skill_id)
        if branch is None:
            raise SkillBranchNotFoundError(f"分支不存在: owner={owner_id} skill={skill_id}")
        return branch

    @staticmethod
    def _require_same_skill(card: SkillCard, skill_id: str) -> None:
        """功能：拒绝把别的技能卡写进本分支（防跨流程串卡）。"""
        if card.skill_id != skill_id:
            raise SkillBranchMismatchError(
                f"卡片 skill_id={card.skill_id} 与目标分支 skill_id={skill_id} 不一致，拒绝写入。"
            )

    def load_global_card(self, skill_id: str) -> SkillCard:
        """功能：读回整体技能库中的卡（分支的回滚/同步基线来源）——缺失与
        存在但非法区分错误语义（不存在 / 非法，不把坏卡误报为不存在）。"""
        path = Path(self.skills_dir).expanduser() / f"{skill_id}.card.json"
        if not path.exists():
            raise SkillBranchNotFoundError(f"整体技能卡不存在: {skill_id}")
        try:
            return load_card(path)
        except SkillCardLoadError as exc:
            raise SkillBranchError(f"整体技能卡非法: {skill_id}: {exc}") from exc

    def _next_unique_branch_version(self, branch: SkillBranch) -> str:
        candidate = next_branch_version(branch.head_version)
        used = {item.version for item in branch.versions}
        while candidate in used:
            candidate = next_global_version(candidate)
        return candidate


def next_global_version(version: str) -> str:
    """功能：整体版本递增——三段式 semver 走 minor+1（patch 归零），否则追加 .1。"""
    parts = version.split(".")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{version}.1"


def next_branch_version(head_version: str, requested: str | None = None) -> str:
    """功能：分支下一版本——显式 requested 合法且不同于当前即采纳，否则 minor+1。"""
    requested_value = (requested or "").strip()
    if _is_semver(requested_value) and requested_value != head_version:
        return requested_value
    return next_global_version(head_version)


def _is_semver(version: str) -> bool:
    parts = version.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _validate_segment(value: str, label: str, error_cls: type[SkillBranchError]) -> None:
    """功能：路径片段安全校验（委托公共件 path_guard）。"""
    validate_path_segment(value, label=label, error_cls=error_cls)


def _read_disk_revision(path: Path) -> int | None:
    """功能：读磁盘分支文件 revision——不存在/不可解析返回 None（按首写处理）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("revision", 0))
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(UTC).isoformat()
