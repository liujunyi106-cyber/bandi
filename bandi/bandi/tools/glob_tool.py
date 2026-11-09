# 功能：Glob 工具（模式匹配文件列表 + limit 截断）
from __future__ import annotations

import glob as _glob
import os
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import (
    project_base,
    project_extra_roots,
    resolve_within_workspace,
    role_fs_scopes,
    sensitive_path_reason,
    workspace_root,
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


class GlobTool(BaseTool):  # 功能：按 glob 模式匹配文件路径，支持递归 ** 和路径限制
    name: str = "glob"
    description: str = "Find files matching a glob pattern (e.g. '**/*.py')"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    needs_context: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["pattern"],
        "additionalProperties": False,
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts')",
            },
            "path": {
                "type": "string",
                "description": "Root directory (default: workspace root)",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
                "default": _DEFAULT_LIMIT,
            },
        },
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        pattern = params["pattern"]
        limit = min(int(params.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)
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

        reason = sensitive_path_reason(root)
        if reason:
            raise ValueError(reason)

        search_path = str(root / pattern) if not Path(pattern).is_absolute() else pattern
        matches: list[str] = []
        for fp in _glob.iglob(search_path, recursive=True):
            if len(matches) >= limit:
                break
            raw = Path(fp)
            raw_abs = raw if raw.is_absolute() else root / raw
            try:
                # `..` 等显式逃逸形态先归一化词法路径——is_relative_to 是词法
                # 检查，`root/../outside` 仍以 root 前缀开头会误判为区内
                normalized = Path(os.path.normpath(raw_abs))
                if not normalized.is_relative_to(root):
                    raise ValueError(f"path outside workspace: {fp}")
                resolved = raw_abs.resolve()
                if not resolved.is_relative_to(root):
                    # 词法在区内但 resolve 出界（symlink/junction
                    # 间接逃逸）→ 跳过该条目，不毁掉整次调用
                    continue
                if sensitive_path_reason(resolved):
                    continue
                matches.append(str(resolved))
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError) and "outside workspace" in str(exc):
                    raise
                continue

        if not matches:
            return "no matches found"

        lines = (
            [f"found {len(matches)} file(s):"]
            if len(matches) < limit
            else [f"found {len(matches)}+ file(s) (showing first {limit}):"]
        )
        lines.extend(f"  {m}" for m in matches)
        return "\n".join(lines)
