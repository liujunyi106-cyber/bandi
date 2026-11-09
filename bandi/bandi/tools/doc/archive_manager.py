# 功能：ArchiveManagerTool — 压缩包管理（list/extract/create/info，zip/tar 系列）

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

_TAR_MODES = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}
_SUFFIX_FORMATS = {
    ".zip": "zip",
    ".tar": "tar",
    ".tar.gz": "tar.gz",
    ".tgz": "tar.gz",
    ".tar.bz2": "tar.bz2",
    ".tar.xz": "tar.xz",
}


def _member_escapes(base: Path, member_path: str) -> bool:
    target = (base / member_path).resolve()
    return not target.is_relative_to(base.resolve())


class ArchiveManagerTool(FsTool):
    name: str = "archive_manager"
    description: str = (
        "Manage archive files: list contents, extract files (with path-traversal protection), "
        "create archives (zip/tar/tar.gz/tar.bz2/tar.xz from files or directories), "
        "and inspect metadata (format, sizes, compression ratio)."
    )
    category: ToolCategory = ToolCategory.DOC
    external_content: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "extract", "create", "info"],
                "description": "Archive operation",
            },
            "archive_path": {"type": "string", "description": "Path to the archive file"},
            "output_dir": {
                "type": "string",
                "description": "Directory to extract into (default: archive stem dir)",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to extract, or paths to include when creating",
            },
            "format": {
                "type": "string",
                "enum": ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz"],
                "description": "Archive format for create (default from extension)",
            },
            "compression_level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 9,
                "description": "Zip compression level 0-9 (default 6)",
            },
        },
        "required": ["operation", "archive_path"],
    }
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        archive_path = params.get("archive_path", "")
        if not archive_path:
            return {"success": False, "error": "archive_path is required"}
        dispatch = {
            "list": self._op_list,
            "extract": self._op_extract,
            "create": self._op_create,
            "info": self._op_info,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _resolve_archive(self, path_str: str, must_exist: bool = True) -> Path:
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if must_exist and not path.is_file():
            raise ValueError(f"archive not found: {path_str}")
        return path

    def _resolve_src(self, path_str: str) -> Path:
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        return path

    def _iter_members(self, archive_path: Path) -> tuple[str, list[dict[str, Any]]]:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                entries = [
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed_size": info.compress_size,
                        "is_dir": info.is_dir(),
                        "date_time": f"{info.date_time[0]:04d}-{info.date_time[1]:02d}-{info.date_time[2]:02d} {info.date_time[3]:02d}:{info.date_time[4]:02d}:{info.date_time[5]:02d}",
                    }
                    for info in zf.infolist()
                ]
            return "zip", entries
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path, "r:*") as tf:
                entries = [
                    {
                        "name": member.name,
                        "size": member.size,
                        "is_dir": member.isdir(),
                        "mode": oct(member.mode) if member.mode else None,
                    }
                    for member in tf.getmembers()
                ]
            return "tar", entries
        raise ValueError(f"unsupported archive format: {archive_path}")

    async def _op_list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_archive(params.get("archive_path", ""))
        fmt, entries = self._iter_members(path)
        return {
            "success": True,
            "archive": str(path),
            "format": fmt,
            "entries": entries,
            "count": len(entries),
        }

    async def _op_extract(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_archive(params.get("archive_path", ""))
        output_dir = self._resolve_safe_path(
            params.get("output_dir") or str(path.parent / path.stem)
        )
        if self._is_sensitive(output_dir):
            raise ValueError(f"sensitive path rejected: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        selected = params.get("files")
        extracted: list[str] = []

        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path, "r") as zf:
                members = selected or [m.filename for m in zf.infolist()]
                for name in members:
                    if _member_escapes(output_dir, name):
                        raise ValueError(f"archive member escapes output dir: {name}")
                    if self._is_sensitive(output_dir / name):
                        raise ValueError(f"sensitive archive member rejected: {name}")
                    try:
                        zf.extract(name, output_dir)
                        extracted.append(name)
                    except KeyError:
                        pass
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as tf:
                if selected:
                    for name in selected:
                        try:
                            member = tf.getmember(name)
                            if _member_escapes(output_dir, member.name):
                                raise ValueError(
                                    f"archive member escapes output dir: {member.name}"
                                )
                            if self._is_sensitive(output_dir / member.name):
                                raise ValueError(
                                    f"sensitive archive member rejected: {member.name}"
                                )
                            tf.extract(member, output_dir, filter="data")
                            extracted.append(name)
                        except KeyError:
                            pass
                else:
                    for member in tf.getmembers():
                        if _member_escapes(output_dir, member.name):
                            raise ValueError(f"archive member escapes output dir: {member.name}")
                        if self._is_sensitive(output_dir / member.name):
                            raise ValueError(f"sensitive archive member rejected: {member.name}")
                    tf.extractall(output_dir, filter="data")
                    extracted = [m.name for m in tf.getmembers()]
        else:
            raise ValueError(f"unsupported archive format: {path}")

        return {
            "success": True,
            "archive": str(path),
            "output_dir": str(output_dir),
            "extracted": extracted,
            "count": len(extracted),
        }

    async def _op_create(self, params: dict[str, Any]) -> dict[str, Any]:
        files = params.get("files")
        if not files:
            raise ValueError("files is required for create")
        path = self._resolve_archive(params.get("archive_path", ""), must_exist=False)
        fmt = params.get("format") or _SUFFIX_FORMATS.get("".join(path.suffixes).lower(), "zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        added: list[str] = []

        if fmt == "zip":
            level = params.get("compression_level", 6)
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
            ) as zf:
                for file_path in files:
                    p = self._resolve_src(str(file_path))
                    if p.is_file():
                        zf.write(p, p.name)
                        added.append(p.name)
                    elif p.is_dir():
                        for child in p.rglob("*"):
                            if child.is_file():
                                arcname = str(child.relative_to(p.parent))
                                zf.write(child, arcname)
                                added.append(arcname)
        else:
            mode = _TAR_MODES.get(fmt, "w:gz")
            with tarfile.open(path, mode) as tf:
                for file_path in files:
                    p = self._resolve_src(str(file_path))
                    if p.exists():
                        tf.add(p, arcname=p.name)
                        added.append(p.name)

        return {
            "success": True,
            "archive": str(path),
            "format": fmt,
            "files_added": added,
            "count": len(added),
            "size": path.stat().st_size,
        }

    async def _op_info(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_archive(params.get("archive_path", ""))
        archive_size = path.stat().st_size
        file_count = 0
        dir_count = 0
        total_uncompressed = 0

        if zipfile.is_zipfile(path):
            fmt = "zip"
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        dir_count += 1
                    else:
                        file_count += 1
                        total_uncompressed += info.file_size
        elif tarfile.is_tarfile(path):
            fmt = "tar"
            with tarfile.open(path, "r:*") as tf:
                for member in tf.getmembers():
                    if member.isdir():
                        dir_count += 1
                    else:
                        file_count += 1
                        total_uncompressed += member.size
        else:
            raise ValueError(f"unsupported archive format: {path}")

        ratio = (
            max(0.0, round(1.0 - archive_size / total_uncompressed, 4))
            if total_uncompressed > 0
            else 0.0
        )
        return {
            "success": True,
            "archive": str(path),
            "format": fmt,
            "archive_size": archive_size,
            "uncompressed_size": total_uncompressed,
            "compression_ratio": ratio,
            "file_count": file_count,
            "dir_count": dir_count,
        }
