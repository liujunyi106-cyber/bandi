# 功能：列目录工具（ListDirTool 条目列举 + 大小/时间）
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import (
    project_base,
    project_extra_roots,
    resolve_within_workspace,
    role_fs_scopes,
    workspace_root,
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500
_KIB = 1024


class ListDirTool(BaseTool):  # 功能：列出目录内容，支持深度限制和文件/目录过滤
    name: str = "list_dir"
    description: str = "List files and directories in a path"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path (default: workspace root)",
            },
            "depth": {
                "type": "integer",
                "description": "Max recursion depth (0 = current dir only, default 1)",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": f"Max entries to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
                "default": _DEFAULT_LIMIT,
            },
        },
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        root_str = params.get("path") or str(workspace_root())
        root = resolve_within_workspace(
            root_str,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
            role_fs_scopes=role_fs_scopes(context),
            scope_base=project_base(context),
        )
        if not root.exists():
            raise FileNotFoundError(f"path not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")

        depth = int(params.get("depth", 1))
        limit = min(int(params.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)
        lines: list[str] = [f"{root}/"]
        count = 0

        def _walk(current: Path, cur_depth: int) -> None:
            nonlocal count
            if count >= limit:
                return
            try:
                entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                lines.append(f"{'  ' * (cur_depth + 1)}[permission denied]")
                return

            for entry in entries:
                if count >= limit:
                    break
                prefix = "  " * (cur_depth + 1)
                # symlink/junction 目录不得递归（is_symlink 对
                # junction 返回 False 不可靠，用 resolve 判定真实位置）
                if entry.is_dir():
                    try:
                        inside = entry.resolve().is_relative_to(root)
                    except OSError:
                        inside = False
                    if not inside:
                        lines.append(f"{prefix}{entry.name}/  (outside link)")
                        count += 1
                        continue
                    lines.append(f"{prefix}{entry.name}/")
                    count += 1
                    if cur_depth < depth:
                        _walk(entry, cur_depth + 1)
                else:
                    try:
                        size = entry.lstat().st_size
                    except OSError:
                        size = 0
                    size_str = f"{size:,}B" if size < _KIB else f"{size / _KIB:,.1f}KB"
                    lines.append(f"{prefix}{entry.name}  ({size_str})")
                    count += 1

        _walk(root, 0)
        if count >= limit:
            lines.append(f"... ({limit}+ entries, truncated)")
        return "\n".join(lines)
