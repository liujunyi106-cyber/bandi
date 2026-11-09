# 功能：folder_tool — 工作区文件夹管理（创建/树/移动/删除/文件读写/搜索；FsTool 沙箱）
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool, project_base, project_extra_roots, resolve_within_workspace


class FolderOperationsTool(FsTool):
    """功能：项目/工作区内文件夹与文件的组织操作（10 个 op，全部经 FsTool 沙箱校验）。"""

    name = "folder_operations"
    description = "文件夹管理：create/list/get_tree/move/delete/add_file/remove_file/search/list_files/read_folder_file"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    risk_level = "high"
    permissions_override = "ask"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "create",
                    "list",
                    "get_tree",
                    "move",
                    "delete",
                    "add_file",
                    "remove_file",
                    "search",
                    "list_files",
                    "read_folder_file",
                ],
                "description": "要执行的操作",
            },
            "name": {"type": "string", "description": "create 目录名（可含相对路径）"},
            "parent": {"type": "string", "description": "create 父目录"},
            "folder": {"type": "string", "description": "目标文件夹"},
            "source": {"type": "string", "description": "move 源路径"},
            "dest": {"type": "string", "description": "move 目标路径"},
            "file": {"type": "string", "description": "add_file/remove_file 文件名"},
            "content": {"type": "string", "description": "add_file 内容"},
            "source_path": {"type": "string", "description": "add_file 外部源文件"},
            "recursive": {"type": "boolean", "description": "delete 是否递归"},
            "query": {"type": "string", "description": "search 关键字"},
        },
        "required": ["operation"],
    }

    def _base_dir(self, context: ToolPermissionContext | None) -> Path:
        roots = project_extra_roots(context)
        return Path(roots[0]).resolve() if roots else self._workspace()

    def _workspace(self) -> Path:
        return Path(
            os.environ.get("BANDI_WORKSPACE_ROOT") or Path.home() / ".bandi" / "workspace"
        ).resolve()

    def _resolve_dir(self, folder: str, context: ToolPermissionContext | None) -> Path:
        if not folder:
            raise ValueError("folder is required")
        return resolve_within_workspace(
            folder, extra_roots=project_extra_roots(context), base=project_base(context)
        )

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ValueError(f"Unknown operation: {op}")
        try:
            return await handler(params, context)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}

    async def _op_create(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            raise ValueError("name is required")
        parent = params.get("parent", "")
        raw = f"{parent.strip('/')}/{name}" if parent else name
        target = resolve_within_workspace(
            raw, extra_roots=project_extra_roots(context), base=project_base(context)
        )
        if self._is_sensitive(target):
            raise ValueError("sensitive path rejected")
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target), "created": target.is_dir()}

    async def _op_list(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        base = self._base_dir(context)
        if not base.is_dir():
            return {"ok": True, "folders": [], "path": str(base)}
        folders = sorted(
            [p.name for p in base.iterdir() if p.is_dir() and not self._is_sensitive(p)]
        )
        return {"ok": True, "folders": folders, "path": str(base)}

    async def _op_get_tree(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        base = self._base_dir(context)
        if not base.is_dir():
            return {"ok": True, "tree": "", "path": str(base)}
        lines: list[str] = []

        def walk(path: Path, depth: int) -> None:
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                if self._is_sensitive(child):
                    continue
                lines.append(f"{'  ' * depth}{child.name}")
                if child.is_dir():
                    walk(child, depth + 1)

        walk(base, 0)
        return {"ok": True, "tree": "\n".join(lines), "path": str(base)}

    async def _op_move(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        source = params.get("source")
        dest = params.get("dest")
        if not source or not dest:
            raise ValueError("source and dest are required")
        src = self._resolve_dir(source, context)
        dst = self._resolve_dir(dest, context)
        if self._is_sensitive(src) or self._is_sensitive(dst):
            raise ValueError("sensitive path rejected")
        if src == dst:
            raise ValueError("source and dest are the same")
        if dst.exists():
            raise ValueError("dest already exists")
        src.rename(dst)
        return {"ok": True, "from": source, "to": dest}

    async def _op_delete(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        folder = params.get("folder")
        if not folder:
            raise ValueError("folder is required")
        target = self._resolve_dir(folder, context)
        if self._is_sensitive(target):
            raise ValueError("sensitive path rejected")
        if not target.exists():
            return {"ok": True, "deleted": False, "path": str(target)}
        if not target.is_dir():
            raise ValueError(f"not a folder: {folder}")
        recursive = params.get("recursive", False)
        children = list(target.iterdir())
        if children and not recursive:
            raise ValueError(f"folder not empty: {folder} (pass recursive=true)")
        if recursive:
            await asyncio.to_thread(_remove_tree, target)
        else:
            await asyncio.to_thread(target.rmdir)
        return {"ok": True, "deleted": True, "path": str(target)}

    async def _op_add_file(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        folder = params.get("folder") or "."
        name = params.get("file")
        if not name:
            raise ValueError("file is required")
        target = resolve_within_workspace(
            f"{folder.strip('/')}/{name}" if folder != "." else name,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
        )
        if self._is_sensitive(target):
            raise ValueError(f"sensitive path rejected: {target}")
        content = params.get("content", "")
        source_path = params.get("source_path")
        if source_path:
            src = resolve_within_workspace(
                source_path, extra_roots=project_extra_roots(context), base=project_base(context)
            )
            if self._is_sensitive(src):
                raise ValueError(f"sensitive path rejected: {source_path}")
            if src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.move, str(src), str(target))
                return {"ok": True, "path": str(target), "bytes": target.stat().st_size}
            raise ValueError(f"source file not found: {source_path}")
        if not content:
            src = resolve_within_workspace(
                name, extra_roots=project_extra_roots(context), base=project_base(context)
            )
            if self._is_sensitive(src):
                raise ValueError(f"sensitive path rejected: {name}")
            if not src.is_file():
                raise ValueError(f"file not found: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(src), str(target))
            return {"ok": True, "path": str(target), "bytes": target.stat().st_size}
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8", newline="")
        return {"ok": True, "path": str(target), "bytes": len(content.encode("utf-8"))}

    async def _op_remove_file(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        folder = params.get("folder") or "."
        name = params.get("file")
        if not name:
            raise ValueError("file is required")
        target = resolve_within_workspace(
            f"{folder.strip('/')}/{name}" if folder != "." else name,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
        )
        if self._is_sensitive(target):
            raise ValueError("sensitive path rejected")
        if not target.is_file():
            raise ValueError(f"file not found: {name}")
        await asyncio.to_thread(target.unlink)
        return {"ok": True, "removed": name}

    async def _op_search(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        query = params.get("query", "")
        base = self._base_dir(context)
        if not base.is_dir():
            return {"ok": True, "matches": []}
        matches: list[str] = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not self._is_sensitive(Path(root) / d)]
            for entry in dirs + files:
                if query and query.lower() not in entry.lower():
                    continue
                matches.append(str(Path(root) / entry))
        return {"ok": True, "matches": sorted(matches)[:200]}

    async def _op_list_files(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        folder = params.get("folder", ".")
        base = self._resolve_dir(folder, context)
        if not base.is_dir():
            return {"ok": True, "files": [], "count": 0}
        files = sorted(
            [p.name for p in base.iterdir() if p.is_file() and not self._is_sensitive(p)]
        )
        return {"ok": True, "files": files, "count": len(files)}

    async def _op_read_folder_file(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        folder = params.get("folder", ".")
        name = params.get("file")
        if not name:
            raise ValueError("file is required")
        target = resolve_within_workspace(
            f"{folder.strip('/')}/{name}" if folder != "." else name,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
        )
        if self._is_sensitive(target):
            raise ValueError(f"sensitive path rejected: {target}")
        if not target.is_file():
            raise ValueError(f"file not found: {name}")
        raw = await asyncio.to_thread(target.read_bytes)
        if self.looks_binary(raw):
            raise ValueError(f"binary file not supported: {name}")
        enc = self.detect_encoding(raw)
        return {"ok": True, "content": raw.decode(enc), "path": str(target)}


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)
