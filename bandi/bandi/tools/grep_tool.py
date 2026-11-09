# 功能：Grep 工具（正则内容搜索 + 结果行号/上下文）
from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import (
    _MAX_FILE_SIZE,
    _SENSITIVE_DIRS_LOWER,
    _SENSITIVE_EXTENSIONS,
    project_base,
    project_extra_roots,
    resolve_within_workspace,
    role_fs_scopes,
    sensitive_path_reason,
    workspace_root,
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


class GrepTool(BaseTool):  # 功能：在文件内容中搜索正则表达式，支持文件过滤/上下文行数/结果数量限制
    name: str = "grep"
    description: str = "Search file contents for a regex pattern (with context lines)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    needs_context: bool = True
    # 安全（③⁻）：命中的文件行可能来自下载/外部携带内容——输出按不可信外部
    # 内容包裹（executor 6.14 机制），读取时无法判定文件来源，不设例外
    external_content: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["pattern"],
        "additionalProperties": False,
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "Root directory (default: workspace root)",
            },
            "include": {
                "type": "string",
                "description": "File glob pattern to filter (e.g. '*.py', '*.{ts,tsx}')",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
                "default": _DEFAULT_LIMIT,
            },
            "context": {
                "type": "integer",
                "description": "Number of context lines before/after each match (default 0)",
                "default": 0,
            },
        },
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        raw_pattern = params["pattern"]
        limit = min(int(params.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)
        ctx = int(params.get("context", 0))
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
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")

        max_regex_len = 500
        if len(raw_pattern) > max_regex_len:
            raise ValueError(f"regex pattern too long (max {max_regex_len} chars)")
        try:
            compiled = re.compile(raw_pattern, re.MULTILINE | re.DOTALL)
        except re.error as e:
            raise ValueError(f"invalid regex pattern: {e}") from e

        include = params.get("include")
        matches: list[tuple[str, int, str]] = []

        for fpath in root.rglob("*"):
            # 后校验真实路径在工作区内，出界即跳过（is_symlink 对 junction
            # 返回 False，不能作为判据）
            try:
                if not fpath.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            if not fpath.is_file():
                continue
            if sensitive_path_reason(fpath):
                continue
            if any(part.lower() in _SENSITIVE_DIRS_LOWER for part in fpath.parts):
                continue
            if fpath.suffix.lower() in _SENSITIVE_EXTENSIONS:
                continue
            if include:
                # `[`/`]` → 字面转义（未转义时 `[ab]` 被当字符类
                # 行首 `?` 引发 re.error 整次 grep 崩溃）
                inc_pattern = (
                    include.replace(".", r"\.")
                    .replace("?", ".")
                    .replace("*", ".*")
                    .replace("[", r"\[")
                    .replace("]", r"\]")
                    .replace("{", "(")
                    .replace("}", ")")
                    .replace(",", "|")
                )
                if not re.match(inc_pattern, fpath.name) and not re.match(
                    inc_pattern, str(fpath.relative_to(root))
                ):
                    continue
            try:
                if fpath.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = fpath.read_text("utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            all_lines = text.splitlines() if ctx > 0 else None

            for m in compiled.finditer(text):
                if len(matches) >= limit:
                    break
                line_num = text[: m.start()].count("\n") + 1
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                if line_end == -1:
                    line_end = len(text)
                preview = text[line_start:line_end].strip()
                matches.append((str(fpath), line_num, preview, all_lines))
            if len(matches) >= limit:
                break

        if not matches:
            return "no matches found"

        lines = (
            [f"found {len(matches)} match(es):"]
            if len(matches) < limit
            else [f"found {len(matches)}+ match(es) (showing first {limit}):"]
        )
        for fpath, lnum, preview, all_lines in matches:
            lines.append(f"{fpath}:{lnum}:{preview}")
            if all_lines is not None:
                start = max(0, lnum - 1 - ctx)
                end = min(len(all_lines), lnum + ctx)
                for ci in range(start, end):
                    marker = ">" if ci == lnum - 1 else " "
                    lines.append(f"  {marker} {ci + 1}: {all_lines[ci]}")
        return "\n".join(lines)
