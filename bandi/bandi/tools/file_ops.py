# 功能：文件读写工具（read/write）— 继承 FsTool 统一沙箱/编码/行号逻辑

from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import _MAX_FILE_SIZE, FsTool, similar_path_suggestions
from bandi.tools.fs_write import (
    atomic_write,
    check_external_modification,
    decode_with_bom,
    file_lock,
)

_DEFAULT_READ_LIMIT = 2000
_MAX_LINE_LENGTH = 2000
_MAX_OUTPUT_BYTES = 50 * 1024
_LINE_TRUNCATED_SUFFIX = "... (line truncated to 2000 chars)"
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def _split_text_lines(text: str) -> list[str]:
    # 功能：只按 CR/LF 分行（与编辑器行号一致）——splitlines 会把 \x0c/\x85 等
    lines = _LINE_SPLIT_RE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _render_read_output(lines: list[str], *, offset: int, limit: int | None) -> str:
    # 功能：行号渲染 + 三态尾注（EOF / 行数帽 / 50KB 字节帽）+ 单行 2000 字符截断
    total = len(lines)
    if total == 0:
        if offset != 1:
            raise ValueError(f"Offset {offset} is out of range for this file (0 lines)")
        return "(End of file - total 0 lines)"
    if offset > total:
        raise ValueError(f"Offset {offset} is out of range for this file ({total} lines)")
    effective_limit = limit if limit is not None else _DEFAULT_READ_LIMIT
    start = offset - 1
    rendered: list[str] = []
    used_bytes = 0
    cut = False
    more = False
    for index in range(start, total):
        if len(rendered) >= effective_limit:
            more = True
            break
        line = lines[index]
        if len(line) > _MAX_LINE_LENGTH:
            line = line[:_MAX_LINE_LENGTH] + _LINE_TRUNCATED_SUFFIX
        numbered = f"{index + 1:>6}|{line}"
        size = len(numbered.encode("utf-8")) + (1 if rendered else 0)
        if used_bytes + size > _MAX_OUTPUT_BYTES:
            cut = True
            break
        rendered.append(numbered)
        used_bytes += size
    last = start + len(rendered)
    if cut:
        note = (
            f"(Output capped at 50 KB. Showing lines {offset}-{last}. "
            f"Use offset={last + 1} to continue.)"
        )
    elif more:
        note = f"(Showing lines {offset}-{last} of {total}. Use offset={last + 1} to continue.)"
    else:
        note = f"(End of file - total {total} lines)"
    body = "\n".join(rendered)
    return f"{body}\n\n{note}" if body else note


class FileReadTool(FsTool):  # 功能：读取文件（支持 offset/limit 分段读取 + 路径安全检查）
    name: str = "read"
    description: str = "Read file contents from disk"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "low"
    is_read_only: bool = True
    # 安全（③⁻）：文件内容可能来自下载/外部携带（第三方文档、仓库外内容）——
    # 输出按不可信外部内容包裹（executor 6.14 机制）；读取时无法可靠判定文件来源
    # 本地受控文件不设例外，不引入开关（保守一律标记）
    external_content: bool = True
    param_aliases: dict[str, str] = {"file_path": "path", "file": "path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["path"],
        "additionalProperties": False,
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (absolute, or relative to workspace root)",
            },
            "file_path": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: auto-detect)",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start from (1-based)",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read",
            },
        },
    }
    is_concurrency_safe: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:  # 功能：读取文件/目录（行号渲染 + 三态尾注 + 50KB 字节帽 + 目录模式）
        raw_path = str(params["path"])
        path = self._resolve_safe_path(params["path"], context)
        if self._is_sensitive(path):
            raise PermissionError(f"access denied: {path} (sensitive path)")
        if not path.exists():
            message = f"file not found: {path}"
            suggestions = similar_path_suggestions(path)
            if suggestions:
                message += "\n\nDid you mean one of these?\n" + "\n".join(suggestions)
            raise FileNotFoundError(message)

        offset = params.get("offset")
        limit = params.get("limit")
        if offset is not None and offset < 1:
            raise ValueError(f"offset must be >= 1, got {offset}")

        if path.is_dir():
            return self._read_directory(path, offset=offset or 1, limit=limit)

        encoding = params.get("encoding")
        raw = await self._read_bytes(path)
        if self.looks_binary(raw, path):
            raise ValueError(
                f"cannot read binary file: {path} (binary/image content is not supported by read)"
            )
        if encoding:
            text = raw.decode(encoding)
        else:
            text = raw.decode(self.detect_encoding(raw))
        self._record_read_state(
            context, raw_path, path, raw, partial=offset is not None or limit is not None
        )
        return _render_read_output(_split_text_lines(text), offset=offset or 1, limit=limit)

    def _read_directory(self, path: Path, *, offset: int, limit: int | None) -> str:
        # 功能：目录模式——条目列表（目录加 /）+ 排序 + 分页尾注
        try:
            entries = sorted(
                entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir()
            )
        except OSError as exc:
            raise ValueError(f"cannot list directory: {path}") from exc
        total = len(entries)
        effective_limit = limit if limit is not None else _DEFAULT_READ_LIMIT
        start = min(offset, total + 1) - 1
        sliced = entries[start : start + max(effective_limit, 0)]
        body = "\n".join(sliced)
        if start + len(sliced) >= total:
            note = f"({total} entries)"
        else:
            note = (
                f"(Showing {offset}-{offset + len(sliced) - 1} of {total} entries. "
                f"Use offset={offset + len(sliced)} to continue.)"
            )
        return f"{body}\n\n{note}" if body else note

    def _record_read_state(
        self,
        context: ToolPermissionContext | None,
        raw_path: str,
        path: Path,
        raw: bytes,
        *,
        partial: bool,
    ) -> None:
        """功能：CE-23——成功读取写入 executor 共享 read_state（键=参数原文
        压缩附件据此按真实读取时间排序；无共享容器/stat 失败时静默跳过）。
        扩展字段（resolved_path/content_sha256/partial）供 Edit 外部修改检测消费。"""
        read_state = (getattr(context, "extra", None) or {}).get("read_state")
        if not isinstance(read_state, dict):
            return
        key = raw_path.strip()
        if not key:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        read_state[key] = {
            "read_at": datetime.now(UTC).isoformat(),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "resolved_path": str(path),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "partial": partial,
        }


class FileWriteTool(FsTool):  # 功能：写入文件（覆盖/追加 + 自动创建父目录 + 路径安全检查）
    name: str = "write"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False, cross_process_resume=True)
    description: str = "Write content to a file (creates or overwrites by default)"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "medium"
    permissions_override: str = "ask"
    param_aliases: dict[str, str] = {"file_path": "path", "file": "path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["path", "content"],
        "additionalProperties": False,
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (absolute, or relative to workspace root)",
            },
            "file_path": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "content": {
                "type": "string",
                "description": "Content to write",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default utf-8)",
                "default": "utf-8",
            },
            "mode": {
                "type": "string",
                "description": "write (overwrite) or append",
                "enum": ["write", "append"],
                "default": "write",
            },
        },
    }
    is_concurrency_safe: bool = False
    needs_context: bool = True

    def resume_eligible(self, arguments: dict[str, Any]) -> bool:
        # 功能：5.2——覆盖写幂等可跨进程恢复；append 非幂等（同参重放会吞
        # 真实追加），必须真实执行
        return (arguments or {}).get("mode", "write") != "append"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:  # 功能：写入/追加文件，自动创建父目录
        path = self._resolve_safe_path(params["path"], context)
        if self._is_sensitive(path):
            raise PermissionError(f"access denied: {path} (sensitive path)")
        content = params["content"]
        encoding = params.get("encoding", "utf-8")
        mode = params.get("mode", "write")

        if len(content.encode("utf-8")) > _MAX_FILE_SIZE:
            raise ValueError(f"file content exceeds max size ({_MAX_FILE_SIZE} bytes)")

        from bandi.agent.file_history import record_file_edit

        record_file_edit(path, context)

        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

        # ① 读后外部修改拒绝（防覆盖用户手改）；② 原 UTF-8 BOM 原样保留；
        # ③ 原子替换统一走 fs_write.atomic_write（tmp + replace）+ per-路径锁
        existing_raw = b""
        if mode != "append" and path.is_file():
            try:
                existing_raw = await asyncio.to_thread(path.read_bytes)
            except OSError:
                existing_raw = b""
        check_external_modification(context, path, existing_raw)
        bom = b""
        if existing_raw and encoding.lower() in ("utf-8", "utf8"):
            _, _, detected_bom = decode_with_bom(existing_raw)
            if detected_bom.startswith(codecs.BOM_UTF8):
                bom = detected_bom

        def _write() -> None:
            atomic_write(path, bom + content.encode(encoding))

        def _append() -> None:
            with path.open("a", encoding=encoding, newline="") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

        if mode == "append":
            async with file_lock(path):
                await asyncio.to_thread(_append)
        else:
            async with file_lock(path):
                await asyncio.to_thread(_write)

        from bandi.agent.file_history import record_file_written

        record_file_written(path, context)

        return f"written {len(content)} bytes to {path}"
