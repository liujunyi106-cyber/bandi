# 功能：SkillCard JSON 序列化（save_card 原子落盘 / load_card 读回 + 闭环校验）。
# （临时目录 → rename 原子替换 + skill_id 路径安全校验）。
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard
from bandi.skills.path_guard import validate_path_segment

_CARD_SUFFIX = ".card.json"
_TEMP_PREFIX = ".{skill_id}.tmp-{pid}-{token}"
_MAX_CARD_BYTES = 256 * 1024


class SkillCardSaveError(BandiError):
    """SkillCard 无法落盘。"""


class SkillCardLoadError(BandiError):
    """SkillCard 无法读回（文件缺失/非法 JSON/违反图闭环/extra 字段）。"""


def save_card(card: SkillCard, skills_dir: str | Path = "~/.bandi/skills") -> Path:
    """功能：将 SkillCard 原子落盘到 skills_dir/<skill_id>.card.json，返回写入路径。"""
    _validate_skill_id(card.skill_id)
    try:
        base = Path(skills_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        raise SkillCardSaveError(f"无法创建技能目录: {skills_dir}: {exc}") from exc
    final_path = base / f"{card.skill_id}{_CARD_SUFFIX}"
    tmp_path = base / _TEMP_PREFIX.format(
        skill_id=card.skill_id, pid=os.getpid(), token=uuid.uuid4().hex[:8]
    )
    try:
        data = json.dumps(card.model_dump(), ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise SkillCardSaveError(f"卡片含不可 JSON 序列化字段: {exc}") from exc
    try:
        tmp_path.write_text(data, encoding="utf-8")
        tmp_path.replace(final_path)
    except (OSError, ValueError) as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SkillCardSaveError(f"写入失败: {final_path}: {exc}") from exc
    return final_path


def load_card(path: str | Path) -> SkillCard:
    """功能：从路径读回 SkillCard（大小上限 → JSON 解析 + pydantic 构造即 validate_graph 闭环）。

    读取捕获 `(OSError, UnicodeError)`：非 UTF-8（GBK 等坏编码）不得穿透为裸
    UnicodeDecodeError 炸掉调用方装载循环；`utf-8-sig` 兼容带 BOM 的卡。
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise SkillCardLoadError(f"无法读取: {target}: {exc}") from exc
    if size > _MAX_CARD_BYTES:
        raise SkillCardLoadError(
            f"卡片超过大小上限 {_MAX_CARD_BYTES} 字节: {target}（{size} 字节）"
        )
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SkillCardLoadError(f"无法读取: {target}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillCardLoadError(f"非法 JSON: {target}: {exc}") from exc
    try:
        return SkillCard(**data)
    except (TypeError, ValueError) as exc:
        raise SkillCardLoadError(f"Schema 校验失败: {target}: {exc}") from exc


def _validate_skill_id(skill_id: str) -> None:
    """功能：skill_id 路径安全校验（委托公共件 path_guard，取 ascii_only 更严口径）。"""
    validate_path_segment(skill_id, label="skill_id", error_cls=SkillCardSaveError, ascii_only=True)
