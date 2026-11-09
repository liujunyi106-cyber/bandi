# 功能：HTMLProcessorTool — HTML 处理（read/extract_links/extract_tables/extract_metadata/convert）

from __future__ import annotations

from html import unescape
from pathlib import Path
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.doc import html_utils as hutils
from bandi.tools.fs_tool import FsTool


class HTMLProcessorTool(FsTool):
    name: str = "html_processor"
    # 安全（③⁻）：HTML 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Read and analyze HTML files: plain text, title, links (with external flag), tables "
        "(with header detection), meta tags (description/keywords/OG/charset), and convert "
        "to Markdown or plain text."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "extract_links", "extract_tables", "extract_metadata", "convert"],
                "description": "Operation to perform",
            },
            "file_path": {"type": "string", "description": "Path to the HTML file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "table_index": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based table index (extract_tables)",
            },
            "output_format": {
                "type": "string",
                "enum": ["markdown", "plain_text"],
                "description": "Target format for convert",
            },
            "output_path": {
                "type": "string",
                "description": "Optional path to write converted output",
            },
        },
        "required": ["operation", "file_path"],
    }
    is_concurrency_safe: bool = False
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        dispatch = {
            "read": self._op_read,
            "extract_links": self._op_links,
            "extract_tables": self._op_tables,
            "extract_metadata": self._op_meta,
            "convert": self._op_convert,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _read_html(self, path_str: str) -> tuple[Path, str]:
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if not path.is_file():
            raise ValueError(f"file not found: {path_str}")
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="latin-1", errors="replace")
        return path, raw

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_html(params.get("file_path", ""))
        p = hutils._ReadParser()
        p.feed(raw)
        p.close()
        text = hutils._normalize_ws(unescape("".join(p.chunks)))
        title = hutils._normalize_ws(unescape(p.title))
        return {
            "success": True,
            "file": str(path),
            "text": text,
            "title": title,
            "word_count": len(text.split()) if text else 0,
            "char_count": len(text),
        }

    async def _op_links(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_html(params.get("file_path", ""))
        p = hutils._LinksParser()
        p.feed(raw)
        p.close()
        return {"success": True, "file": str(path), "links": p.links, "count": len(p.links)}

    async def _op_tables(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_html(params.get("file_path", ""))
        p = hutils._TablesParser()
        p.feed(raw)
        p.close()
        tables = p.tables
        table_index = params.get("table_index")
        if table_index is not None:
            if table_index < 0 or table_index >= len(tables):
                raise ValueError(f"table_index {table_index} out of range (0..{len(tables) - 1})")
            tables = [tables[table_index]]
        return {"success": True, "file": str(path), "tables": tables, "count": len(tables)}

    async def _op_meta(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_html(params.get("file_path", ""))
        p = hutils._MetaParser()
        p.feed(raw)
        p.close()
        return {
            "success": True,
            "file": str(path),
            "title": hutils._normalize_ws(unescape(p.title)),
            "description": p.description,
            "keywords": p.keywords,
            "og_tags": p.og_tags,
            "charset": p.charset,
            "other_meta": p.other_meta,
        }

    async def _op_convert(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_html(params.get("file_path", ""))
        fmt = params.get("output_format", "")
        if fmt not in ("markdown", "plain_text"):
            raise ValueError("output_format must be markdown or plain_text")
        if fmt == "plain_text":
            p = hutils._ReadParser()
            p.feed(raw)
            p.close()
            content = hutils._normalize_ws(unescape("".join(p.chunks)))
        else:
            content = hutils.html_to_markdown(raw)
        result: dict[str, Any] = {
            "success": True,
            "file": str(path),
            "content": content,
            "output_format": fmt,
        }
        output_path = params.get("output_path")
        if output_path:
            out = self._write_output(output_path, content)
            result["output_path"] = str(out)
        return result

    def _write_output(self, path_str: str, content: str) -> Path:
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        path.parent.mkdir(parents=True, exist_ok=True)
        from bandi.tools.fs_write import atomic_write

        atomic_write(path, content.encode("utf-8"))
        return path
