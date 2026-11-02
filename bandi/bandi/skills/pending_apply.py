# 功能：技能暂存重放落盘器——把待批准 SKILL.md 全文原子写入技能目录
# （临时文件 + os.replace + UTF-8；路径片段经 path_guard 校验，失败安全返回 False）
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from bandi.core.exceptions import BandiError
from bandi.skills.importer import SkillImportError
from bandi.skills.path_guard import validate_path_segment

logger = logging.getLogger(__name__)


def default_skills_dir() -> Path:
    """功能：默认技能库目录（与 SkillImporter/演进直写同一解析）。"""
    from bandi.skills.importer import default_skills_dir as importer_skills_dir

    return Path(importer_skills_dir())


def write_skill_md(skills_dir: str | Path, skill_name: str, content: str) -> bool:
    """功能：原子写 `<skills_dir>/<skill_name>/SKILL.md`（UTF-8）；失败返回 False。

    内容即 SKILL.md 全文（含 frontmatter）——暂存 payload 原样落盘，不做二次加工。
    """
    try:
        safe_name = validate_path_segment(
            skill_name, label="skill_name", error_cls=SkillImportError
        )
    except BandiError as exc:
        logger.warning("skill replay rejected unsafe name %r: %s", skill_name, exc)
        return False
    text = str(content or "")
    if not text.strip():
        return False
    skill_dir = Path(skills_dir) / safe_name
    temp_path: Path | None = None
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".SKILL-", suffix=".md.tmp", dir=str(skill_dir))
        temp_path = Path(temp_name)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(str(temp_path), str(skill_dir / "SKILL.md"))
        return True
    except (OSError, ValueError) as exc:
        logger.warning("skill replay write failed (%s): %s", safe_name, exc)
        return False
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
