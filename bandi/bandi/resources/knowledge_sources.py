# 功能：知识文档源文件目录（上传/编辑保留原始 Markdown，卸载移入归档）。
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class KnowledgeSourceError(Exception):
    """知识文档源文件路径不合法或读写失败。"""


def docs_root() -> Path:
    """功能：知识文档源文件根目录（默认 ~/.bandi/knowledge/docs）。"""
    override = os.environ.get("BANDI_KNOWLEDGE_DOCS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bandi" / "knowledge" / "docs"


def _safe_base_id(base_id: str) -> str:
    """功能：base_id 转为安全目录名；发生替换时追加短哈希防碰撞。"""
    raw = str(base_id or "").strip() or "shared"
    slug = _SAFE_SEGMENT_RE.sub("_", raw).strip("._") or "shared"
    if slug != raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[:48]}-{digest}"
    return slug[:80]


def _safe_filename(filename: str) -> str:
    """功能：文件名只保留最后一段，拒绝空名与路径穿越。"""
    raw = str(filename or "").replace("\\", "/").strip()
    name = Path(raw).name
    if not name or name in {".", ".."} or "\x00" in name:
        raise KnowledgeSourceError(f"非法文件名: {filename!r}")
    return name


def base_dir(base_id: str, *, create: bool = False) -> Path:
    """功能：知识库对应的源文件目录；create=True 时才创建目录。"""
    target = docs_root() / _safe_base_id(base_id)
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def source_path(base_id: str, filename: str, *, create: bool = False) -> Path:
    """功能：知识文档源文件路径；create=True 时才创建父目录。"""
    return base_dir(base_id, create=create) / _safe_filename(filename)


def write_source(base_id: str, filename: str, text: str) -> Path:
    """功能：原子写入知识文档源文件。"""
    target = source_path(base_id, filename, create=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise KnowledgeSourceError(f"写入知识文档源文件失败: {exc}") from exc
    return target


def read_source(base_id: str, filename: str) -> str | None:
    """功能：读取知识文档源文件；不存在返回 None。"""
    target = source_path(base_id, filename)
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            return target.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError):
            return None
    except OSError:
        return None


def archive_source(base_id: str, filename: str) -> Path | None:
    """功能：把源文件移入归档目录（不删除本地内容）。"""
    target = source_path(base_id, filename)
    if not target.is_file():
        return None
    archive_dir = Path.home() / ".bandi" / "_uninstalled" / "knowledge" / _safe_base_id(base_id)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    archived = archive_dir / f"{target.name}-{stamp}"
    index = 1
    while archived.exists():
        archived = archive_dir / f"{target.name}-{stamp}-{index}"
        index += 1
    try:
        shutil.move(str(target), str(archived))
    except OSError as exc:
        raise KnowledgeSourceError(f"归档知识文档失败: {exc}") from exc
    return archived
