# 功能：技能导入（从目录/SKILL.md 导入技能包，含校验/资源复制/原子替换）
# + 资源目录复制（scripts/references/assets 原样 + 其余 fallback）+ 原子替换
from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bandi.core.exceptions import BandiError
from bandi.skills.path_guard import validate_path_segment

ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "metadata",
    "always",
    "license",
    "allowed-tools",
    "homepage",
    "domains",
    "triggers",
    "instructions",
    "constraints",
    # .7：学习型技能版本号（_save_skill_md 写入，合并更新时递增）
    "version",
    "team_id",
    "enabled",
}
ALLOWED_RESOURCE_DIRS = {"scripts", "references", "assets"}
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION_LENGTH = 1024

_FRONTMATTER_BLOCK_RE = re.compile(r"^(\ufeff?\s*---\s*\n)(.*?)(\n---\s*\n)", re.DOTALL)


class SkillImportError(BandiError):
    """Skill import failed."""


@dataclass
class SkillImportResult:
    skill_name: str
    skill_path: str
    message: str
    enabled_domains: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def default_skills_dir() -> str:
    """功能：默认技能目录（惰性解析——防收集期 import 固化真实 home）。"""
    return str(Path.home() / ".bandi" / "skills")


class SkillImporter:
    # 功能：从本地 path 导入 SKILL.md 技能到技能库目录（幂等覆盖 + 原子替换）
    def __init__(self, skills_dir: str | None = None) -> None:
        self._skills_dir = skills_dir or default_skills_dir()

    def import_skill(self, path: str, team_id: str = "") -> SkillImportResult:
        # 功能：从本地 path 导入 SKILL.md 技能到技能库目录（幂等覆盖 + 原子替换）；
        source = Path(path)
        if not source.exists():
            raise SkillImportError(f"skill path does not exist: {path}")
        skill_md = source if source.is_file() else source / "SKILL.md"
        if not skill_md.is_file():
            raise SkillImportError(f"no SKILL.md found at {path}")
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillImportError(f"failed to read SKILL.md: {exc}") from exc
        text = self._apply_team_id(text, team_id)

        frontmatter = self._parse_frontmatter(text)
        raw_name = frontmatter.get("name")
        name = str(raw_name).strip() if raw_name is not None else ""
        name = name or skill_md.parent.name
        if not name:
            raise SkillImportError("skill has no name")
        self._validate_skill_name(name)
        description = str(frontmatter.get("description", "")).strip()
        self._validate_description(description, name)

        base = self._target_base()
        target_dir = base / name
        prepare_dir = base / f".{name}.import-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            self._stage_to_prepare(skill_md, source, prepare_dir, text, name)
            self._atomic_replace(prepare_dir, target_dir)
        except OSError as exc:
            self._cleanup(prepare_dir)
            raise SkillImportError(f"failed to write skill: {exc}") from exc
        finally:
            self._cleanup(prepare_dir)

        self._readback_verify(name, target_dir)

        domains = frontmatter.get("domains") or []
        if isinstance(domains, str):
            domains = [domains]
        enabled_domains = [str(d).strip() for d in domains if str(d).strip()]
        warnings: list[str] = []
        for key in frontmatter:
            if key not in ALLOWED_FRONTMATTER_KEYS:
                warnings.append(f"unknown frontmatter key preserved: {key}")
        return SkillImportResult(
            skill_name=name,
            skill_path=str(target_dir / "SKILL.md"),
            message=f"imported skill `{name}` from {path}",
            enabled_domains=enabled_domains,
            warnings=warnings,
        )

    @classmethod
    def _apply_team_id(cls, text: str, team_id: str) -> str:
        """功能：带属导入——frontmatter 写入 team_id（覆盖已有值；无 fm 时创建块）。

        team_id 经路径安全校验（拒绝穿越/空白/换行注入），非法显式失败零写盘。
        """
        tid = str(team_id or "").strip()
        if not tid:
            return text
        tid = validate_path_segment(tid, label="team_id", error_cls=SkillImportError)
        match = _FRONTMATTER_BLOCK_RE.match(text)
        if match is None:
            return f"---\nteam_id: {tid}\n---\n\n{text}"
        body = match.group(2)
        if re.search(r"^team_id\s*:\s*[|>]", body, re.M):
            raise SkillImportError("team_id 不支持多行块标量（YAML |/>）")
        if re.search(r"^team_id\s*:", body, re.M):
            # 全文替换：重复键若只改首行，YAML 后者覆盖会让新归属静默失效
            body = re.sub(r"^team_id\s*:.*$", f"team_id: {tid}", body, flags=re.M)
        else:
            body = f"team_id: {tid}\n{body}"
        return match.group(1) + body + match.group(3) + text[match.end() :]

    def _target_base(self) -> Path:
        try:
            base = Path(self._skills_dir).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise SkillImportError(f"invalid skills dir: {self._skills_dir}") from exc
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _stage_to_prepare(
        self,
        skill_md: Path,
        source: Path,
        prepare_dir: Path,
        text: str,
        name: str,
    ) -> None:
        if prepare_dir.exists():
            shutil.rmtree(prepare_dir, ignore_errors=True)
        prepare_dir.mkdir(parents=True, exist_ok=True)
        (prepare_dir / "SKILL.md").write_text(text, encoding="utf-8")
        if source.is_file():
            return
        for entry in sorted(source.iterdir()):
            if entry.name == "SKILL.md":
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir() and entry.name in ALLOWED_RESOURCE_DIRS:
                shutil.copytree(entry, prepare_dir / entry.name, dirs_exist_ok=True)
            else:
                fallback = prepare_dir / "assets" / "imported-root"
                fallback.mkdir(parents=True, exist_ok=True)
                dest = fallback / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(entry, dest)

    def _atomic_replace(self, prepare_dir: Path, target_dir: Path) -> None:
        if target_dir.exists():
            backup = target_dir.parent / (
                f".{target_dir.name}.backup-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            target_dir.rename(backup)
            try:
                prepare_dir.rename(target_dir)
            except OSError:
                backup.rename(target_dir)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            prepare_dir.rename(target_dir)

    @staticmethod
    def _cleanup(dir_path: Path) -> None:
        if dir_path.exists():
            shutil.rmtree(dir_path, ignore_errors=True)

    def _readback_verify(self, name: str, target_dir: Path) -> None:
        md = target_dir / "SKILL.md"
        if not md.is_file():
            raise SkillImportError(f"imported skill missing SKILL.md: {name}")
        try:
            data = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillImportError(f"imported skill unreadable: {name}") from exc
        if not data.strip():
            raise SkillImportError(f"imported skill is empty: {name}")

    def _validate_skill_name(self, name: str) -> None:
        if name in (".", "..") or "/" in name or "\\" in name:
            raise SkillImportError(f"invalid skill name: {name!r}")
        if len(name) > MAX_SKILL_NAME_LENGTH:
            raise SkillImportError(f"skill name too long: {name!r}")
        if re.search(r"[\x00-\x1f\x7f]", name):
            raise SkillImportError(f"skill name has control chars: {name!r}")

    def _validate_description(self, description: str, name: str) -> None:
        if not description:
            return
        lowered = description.lower()
        if "[todo" in lowered or "todo:" in lowered:
            raise SkillImportError(f"skill `{name}` description has TODO placeholder")
        if "<" in description or ">" in description:
            raise SkillImportError(f"skill `{name}` description has angle brackets")
        if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
            raise SkillImportError(f"skill `{name}` description too long")

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        fm_match = re.match(r"^\ufeff?\s*---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not fm_match:
            return {}
        try:
            data = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            raise SkillImportError("malformed frontmatter in SKILL.md") from None
        return data if isinstance(data, dict) else {}
