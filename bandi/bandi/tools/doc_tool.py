# 功能：DocumentReadTool — 文档读取工具（markdown/txt/csv/pdf），沙箱+敏感路径校验

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

_MAX_TEXT_CHARS = 200_000
_CSV_MAX_ROWS = 200

logger = logging.getLogger(__name__)


class DocumentReadTool(FsTool):
    name: str = "read_document"
    description: str = (
        "Read a document file and return its parsed content. Supported formats: "
        "markdown/txt (plain text), csv (columns + rows), pdf (extracted text). "
        "Detects format by file extension unless 'format' is given."
    )
    category: ToolCategory = ToolCategory.DOC
    is_read_only: bool = True
    # 安全（③⁻）：文档内容可能来自下载/外部携带（pdf/md/csv 等）——输出按
    # 不可信外部内容包裹（executor 6.14 机制），读取时无法判定文件来源，一律标记
    external_content: bool = True
    # 弱模型参数名漂移容错（同族）：doc_path/file_path/file 等价 path
    param_aliases: dict[str, str] = {"file_path": "path", "doc_path": "path", "file": "path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the document file (relative to workspace or absolute)",
            },
            "doc_path": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "file_path": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'path' — accepted interchangeably",
            },
            "format": {
                "type": "string",
                "enum": ["auto", "markdown", "csv", "pdf"],
                "description": "Optional format override. Default: auto (by extension)",
            },
            "offset": {
                "type": "integer",
                "description": ("起始偏移：文本/PDF 为字符偏移，CSV 为起始行号（默认 0）"),
            },
            "max_chars": {
                "type": "integer",
                "description": "本次最多返回字符数（文本/PDF；默认 200000，上限同）",
            },
            "max_rows": {
                "type": "integer",
                "description": "本次最多返回行数（CSV；默认 200）",
            },
        },
        "required": ["path"],
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("path", "")
        if not path_str:
            return {"success": False, "error": "path is required"}
        try:
            path = self._resolve_safe_path(path_str, context)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if self._is_sensitive(path):
            return {"success": False, "error": f"sensitive path rejected: {path_str}"}
        if not path.is_file():
            return {"success": False, "error": f"file not found: {path_str}"}

        fmt = params.get("format") or "auto"
        if fmt == "auto":
            fmt = self._detect_format(path.name)
        try:
            raw = await self._read_bytes(path)
        except OSError as e:
            return {"success": False, "error": f"read failed: {e}"}

        try:
            offset = max(0, int(params.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            max_chars = int(params.get("max_chars") or _MAX_TEXT_CHARS)
        except (TypeError, ValueError):
            max_chars = _MAX_TEXT_CHARS
        max_chars = max(1, min(max_chars, _MAX_TEXT_CHARS))
        try:
            max_rows = int(params.get("max_rows") or _CSV_MAX_ROWS)
        except (TypeError, ValueError):
            max_rows = _CSV_MAX_ROWS
        max_rows = max(1, max_rows)

        if fmt == "csv":
            return self._read_csv(raw, offset=offset, max_rows=max_rows)
        if fmt == "pdf":
            return self._read_pdf(path, offset=offset, max_chars=max_chars)
        return self._read_text(raw, offset=offset, max_chars=max_chars)

    @staticmethod
    def _detect_format(filename: str) -> str:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix == "csv":
            return "csv"
        if suffix == "pdf":
            return "pdf"
        if suffix in ("md", "markdown", "txt"):
            return "markdown"
        return "markdown"

    def _read_text(
        self, raw: bytes, *, offset: int = 0, max_chars: int = _MAX_TEXT_CHARS
    ) -> dict[str, Any]:
        if self.looks_binary(raw):
            return {"success": False, "error": "binary file cannot be read as text"}
        encoding = self.detect_encoding(raw)
        text = raw.decode(encoding, errors="replace")
        total = len(text)
        window = text[offset : offset + max_chars]
        truncated = (offset + len(window)) < total
        return {
            "success": True,
            "format": "text",
            "content": window,
            "encoding": encoding,
            "line_count": window.count("\n") + 1,
            "truncated": truncated,
            "offset": offset,
            "total_chars": total,
            "next_offset": (offset + len(window)) if truncated else None,
        }

    def _read_csv(
        self, raw: bytes, *, offset: int = 0, max_rows: int = _CSV_MAX_ROWS
    ) -> dict[str, Any]:
        if self.looks_binary(raw):
            return {"success": False, "error": "binary file cannot be read as csv"}
        encoding = self.detect_encoding(raw)
        text = raw.decode(encoding, errors="replace")
        reader = csv.reader(io.StringIO(text))
        try:
            headers = next(reader)
        except StopIteration:
            return {"success": True, "format": "csv", "columns": [], "rows": [], "row_count": 0}
        all_rows = list(reader)
        window = all_rows[offset : offset + max_rows]
        truncated = (offset + len(window)) < len(all_rows)
        return {
            "success": True,
            "format": "csv",
            "columns": headers,
            "rows": window,
            "row_count": len(window),
            "total_rows": len(all_rows),
            "truncated": truncated,
            "offset": offset,
            "next_offset": (offset + len(window)) if truncated else None,
        }

    def _read_pdf(
        self, path, *, offset: int = 0, max_chars: int = _MAX_TEXT_CHARS
    ) -> dict[str, Any]:
        try:
            from pypdf import PdfReader
        except ImportError:
            return {
                "success": False,
                "error": "pdf reading requires pypdf; install it in the environment",
            }
        try:
            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages]
            content = "\n\n".join(pages)
            total = len(content)
            window = content[offset : offset + max_chars]
            truncated = (offset + len(window)) < total
            return {
                "success": True,
                "format": "pdf",
                "content": window,
                "page_count": len(pages),
                "truncated": truncated,
                "offset": offset,
                "total_chars": total,
                "next_offset": (offset + len(window)) if truncated else None,
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("pdf read failed: %s", e)
            return {"success": False, "error": f"pdf read failed: {e}"}
