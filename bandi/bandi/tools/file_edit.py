# 功能：文件编辑工具（FileEditTool 回退匹配链 + 行尾/BOM 保真 + 外部修改检测 + 原子写）
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.edit_match import (
    convert_line_ending,
    detect_line_ending,
    normalize_line_endings,
    replace_with_fallback,
)
from bandi.tools.fs_tool import _MAX_FILE_SIZE, similar_path_suggestions
from bandi.tools.fs_write import (
    atomic_write,
    check_external_modification,
    decode_with_bom,
    file_lock,
)


class FileEditTool(BaseTool):
    # 功能：回退匹配链编辑文件（行尾/BOM 保真 + 外部修改检测 + 原子写 + 文件级锁）
    name: str = "edit"
    description: str = (
        "Edit a file by replacing text. Fallback matching tolerates minor "
        "whitespace/indentation/quote differences; ambiguous or disproportionate "
        "matches are rejected. Use replace_all=True to replace all occurrences."
    )
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "medium"
    permissions_override: str = "ask"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (absolute, or relative to workspace root)",
            },
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find (fallback matching tolerates minor whitespace, "
                    "indentation, and quote-style drift; must be unique unless replace_all)"
                ),
            },
            "new_string": {
                "type": "string",
                "description": "Text to replace with",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default false)",
                "default": False,
            },
        },
    }
    is_concurrency_safe: bool = False
    needs_context: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        file_path = params["file_path"]
        old = params["old_string"]
        new = params["new_string"]
        replace_all = params.get("replace_all", False)

        if old == new:
            raise ValueError("No changes to apply: old_string and new_string are identical.")

        from bandi.tools.fs_tool import (
            project_base,
            project_extra_roots,
            resolve_within_workspace,
            role_fs_scopes,
            sensitive_path_reason,
        )

        target = resolve_within_workspace(
            file_path,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
            role_fs_scopes=role_fs_scopes(context),
        )
        reason = sensitive_path_reason(target)
        if reason:
            raise ValueError(reason)
        if not target.exists():
            message = f"file not found: {file_path}"
            suggestions = similar_path_suggestions(target)
            if suggestions:
                message += f". Did you mean: {', '.join(suggestions)}"
            raise FileNotFoundError(message)
        if not target.is_file():
            raise IsADirectoryError(f"not a file: {file_path}")

        async with file_lock(target):
            return await asyncio.to_thread(self._edit_file, target, old, new, replace_all, context)

    @staticmethod
    def _edit_file(
        target: Path,
        old: str,
        new: str,
        replace_all: bool,
        context: ToolPermissionContext | None,
    ) -> str:
        # 功能：锁内同步编辑（bytes 读 → 外部修改检测 → 替换 → BOM/编码保真写回）
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ValueError(f"cannot stat file: {target}") from exc
        if size > _MAX_FILE_SIZE:
            raise ValueError(f"file exceeds max size ({_MAX_FILE_SIZE} bytes)")
        raw = target.read_bytes()
        check_external_modification(context, target, raw)
        text, encoding, bom = decode_with_bom(raw)

        if old == "":
            if text.strip() != "":
                raise ValueError(
                    "old_string cannot be empty when editing a non-empty file. "
                    "Provide the exact text to replace, or use write for a full-file "
                    "replacement."
                )
            new_text = new
            count = 1
        else:
            ending = detect_line_ending(text)
            converted_old = convert_line_ending(normalize_line_endings(old), ending)
            converted_new = convert_line_ending(normalize_line_endings(new), ending)
            match = replace_with_fallback(text, converted_old, converted_new, replace_all)
            new_text = match.text
            count = match.count

        from bandi.agent.file_history import record_file_edit, record_file_written
        from bandi.tools.file_ops import _refresh_skill_registry_if_skill_file

        record_file_edit(target, context)
        atomic_write(target, bom + new_text.encode(encoding))
        record_file_written(target, context)
        _refresh_skill_registry_if_skill_file(target)
        return f"replaced {count} occurrence(s) in {target}"
