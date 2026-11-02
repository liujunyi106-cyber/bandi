# 功能：MarkdownProcessorTool — Markdown 文档处理器（17 操作 + 7 模板；解析/渲染见 markdown_utils.py）

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.doc import markdown_utils as md_utils
from bandi.tools.fs_tool import FsTool


class MarkdownProcessorTool(FsTool):
    name: str = "markdown_processor"
    # 安全（③⁻）：Markdown 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Professional Markdown document processor: read/parse structure (headings, links, images, "
        "code blocks, frontmatter), write/create/append/prepend files, insert/replace/delete sections, "
        "extract/generate TOC, extract code blocks, build tables and lists, merge files, format, "
        "convert to HTML/plain text, and generate documents from templates "
        "(story/report/notes/article/meeting/readme/changelog)."
    )
    category: ToolCategory = ToolCategory.DOC
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "read",
                    "write",
                    "create",
                    "append",
                    "prepend",
                    "insert_section",
                    "replace_section",
                    "delete_section",
                    "extract_toc",
                    "generate_toc",
                    "extract_code_blocks",
                    "build_table",
                    "build_list",
                    "merge",
                    "format",
                    "convert",
                    "template",
                ],
                "description": "Operation to perform",
            },
            "file_path": {
                "type": "string",
                "description": "Markdown file path: existing file for read/write/format/convert; destination for create/append/prepend/insert/replace/delete",
            },
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "content": {
                "type": "string",
                "description": "Markdown content for write/append/prepend/insert/replace operations",
            },
            "title": {"type": "string", "description": "Document title for create/template"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "level": {"type": "integer"},
                        "content": {"type": "string"},
                    },
                },
                "description": "Sections for create: each has heading (default level 2) and content",
            },
            "metadata": {
                "type": "object",
                "description": "Frontmatter key-value pairs for create/template",
            },
            "section_title": {
                "type": "string",
                "description": "Target heading for insert_section/replace_section/delete_section",
            },
            "position": {
                "type": "string",
                "enum": ["before", "after", "replace"],
                "description": "Insert position relative to section_title (default after)",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "description": "Max heading depth for TOC (default 3)",
            },
            "language": {
                "type": "string",
                "description": "Language filter for extract_code_blocks",
            },
            "output_format": {
                "type": "string",
                "enum": ["html", "plain_text"],
                "description": "Target format for convert",
            },
            "output_path": {
                "type": "string",
                "description": "Optional path to write converted/TOC output",
            },
            "merge_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source files to merge",
            },
            "separator": {
                "type": "string",
                "description": "Separator between merged files (default '\\n\\n---\\n\\n')",
            },
            "template_name": {
                "type": "string",
                "enum": ["story", "report", "notes", "article", "meeting", "readme", "changelog"],
                "description": "Template to use for template operation",
            },
            "template_data": {"type": "object", "description": "Data to populate template fields"},
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Column headers for build_table",
            },
            "rows": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "Table rows for build_table",
            },
            "align": {
                "type": "string",
                "enum": ["left", "center", "right"],
                "description": "Table alignment (default left)",
            },
            "items": {
                "type": "array",
                "description": "Items for build_list (strings or {text, children})",
            },
            "ordered": {
                "type": "boolean",
                "description": "Ordered list for build_list (default false)",
            },
        },
        "required": ["operation"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        dispatch = {
            "read": self._op_read,
            "write": self._op_write,
            "create": self._op_create,
            "append": self._op_append,
            "prepend": self._op_prepend,
            "insert_section": self._op_insert_section,
            "replace_section": self._op_replace_section,
            "delete_section": self._op_delete_section,
            "extract_toc": self._op_extract_toc,
            "generate_toc": self._op_generate_toc,
            "extract_code_blocks": self._op_extract_code_blocks,
            "build_table": self._op_build_table,
            "build_list": self._op_build_list,
            "merge": self._op_merge,
            "format": self._op_format,
            "convert": self._op_convert,
            "template": self._op_template,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    # ── 路径辅助 ──────────────────────────────────────────────────────

    def _read_file(self, path_str: str) -> tuple[Path, str]:
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if not path.is_file():
            raise ValueError(f"file not found: {path_str}")
        return path, path.read_text(encoding="utf-8", errors="replace")

    def _write_file(self, path_str: str, content: str) -> Path:
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        path.parent.mkdir(parents=True, exist_ok=True)
        from bandi.tools.fs_write import atomic_write

        atomic_write(path, content.encode("utf-8"))
        return path

    # ── 17 操作 ───────────────────────────────────────────────────────

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        lines = raw.splitlines()
        return {
            "success": True,
            "file": str(path),
            "raw_text": raw,
            "headings": md_utils.parse_headings(lines),
            "links": md_utils.parse_links(raw),
            "images": md_utils.parse_images(raw),
            "code_blocks": md_utils.parse_code_blocks(raw),
            "frontmatter": md_utils.parse_frontmatter(raw),
            "word_count": len(re.findall(r"\S+", raw)),
            "line_count": len(lines),
            "char_count": len(raw),
        }

    async def _op_write(self, params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        if content is None:
            raise ValueError("content is required for write")
        path = self._write_file(params.get("file_path", ""), content)
        return {
            "success": True,
            "file": str(path),
            "chars_written": len(content),
            "size_bytes": path.stat().st_size,
        }

    async def _op_create(self, params: dict[str, Any]) -> dict[str, Any]:
        rendered = md_utils._render_document(params)
        path = self._write_file(params.get("file_path", ""), rendered)
        return {
            "success": True,
            "file": str(path),
            "chars_written": len(rendered),
            "sections_count": len(params.get("sections") or []),
            "has_frontmatter": params.get("metadata") is not None,
        }

    async def _op_append(self, params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        if content is None:
            raise ValueError("content is required for append")
        path_str = params.get("file_path", "")
        existing = ""
        if path_str:
            try:
                _, existing = self._read_file(path_str)
            except ValueError:
                pass
        separator = "" if not existing or existing.endswith("\n\n") else "\n\n"
        full = existing + separator + content
        path = self._write_file(path_str, full)
        return {
            "success": True,
            "file": str(path),
            "total_chars": len(full),
            "appended_chars": len(content),
        }

    async def _op_prepend(self, params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        if content is None:
            raise ValueError("content is required for prepend")
        path_str = params.get("file_path", "")
        existing = ""
        if path_str:
            try:
                _, existing = self._read_file(path_str)
            except ValueError:
                pass
        separator = "" if not existing or existing.startswith("\n") else "\n\n"
        path = self._write_file(path_str, content + separator + existing)
        return {
            "success": True,
            "file": str(path),
            "total_chars": path.stat().st_size,
            "prepended_chars": len(content),
        }

    async def _op_insert_section(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        section_title = params.get("section_title", "")
        content = params.get("content")
        if not section_title:
            raise ValueError("section_title is required")
        if content is None:
            raise ValueError("content is required")
        lines = raw.splitlines()
        rng = md_utils.find_section_range(lines, section_title)
        if rng is None:
            raise ValueError(f"section not found: {section_title}")
        start, end = rng
        insert_lines = content.splitlines()
        position = params.get("position", "after")
        if position == "before":
            new_lines = lines[:start] + [""] + insert_lines + [""] + lines[start:]
        elif position == "replace":
            new_lines = lines[:start] + insert_lines + lines[end:]
        else:
            new_lines = lines[:end] + [""] + insert_lines + [""] + lines[end:]
        self._write_file(str(path), "\n".join(new_lines))
        return {
            "success": True,
            "file": str(path),
            "position": position,
            "section_title": section_title,
        }

    async def _op_replace_section(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        section_title = params.get("section_title", "")
        content = params.get("content")
        if not section_title:
            raise ValueError("section_title is required")
        if content is None:
            raise ValueError("content is required")
        lines = raw.splitlines()
        rng = md_utils.find_section_range(lines, section_title)
        if rng is None:
            raise ValueError(f"section not found: {section_title}")
        start, end = rng
        replacement_lines = [lines[start], ""] + content.splitlines()
        self._write_file(str(path), "\n".join(lines[:start] + replacement_lines + lines[end:]))
        return {"success": True, "file": str(path), "section_title": section_title}

    async def _op_delete_section(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        section_title = params.get("section_title", "")
        if not section_title:
            raise ValueError("section_title is required")
        lines = raw.splitlines()
        rng = md_utils.find_section_range(lines, section_title)
        if rng is None:
            raise ValueError(f"section not found: {section_title}")
        start, end = rng
        self._write_file(str(path), "\n".join(lines[:start] + lines[end:]))
        return {
            "success": True,
            "file": str(path),
            "section_title": section_title,
            "lines_removed": end - start,
        }

    async def _op_extract_toc(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        max_depth = int(params.get("max_depth", 3))
        headings = md_utils.parse_headings(raw.splitlines())
        return {
            "success": True,
            "file": str(path),
            "toc": md_utils.nest_toc(headings, max_depth),
            "max_depth": max_depth,
            "heading_count": len(headings),
        }

    async def _op_generate_toc(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        max_depth = int(params.get("max_depth", 3))
        toc_lines: list[str] = []
        for h in md_utils.parse_headings(raw.splitlines()):
            if h["level"] > max_depth:
                continue
            indent = "  " * (h["level"] - 1)
            anchor = re.sub(r"[^\w\s-]", "", h["text"].lower())
            anchor = re.sub(r"\s+", "-", anchor.strip())
            toc_lines.append(f"{indent}- [{h['text']}](#{anchor})")
        toc_md = "\n".join(toc_lines)
        result: dict[str, Any] = {
            "success": True,
            "file": str(path),
            "toc_markdown": toc_md,
            "heading_count": len(toc_lines),
        }
        output_path = params.get("output_path")
        if output_path:
            out = self._write_file(output_path, toc_md + "\n")
            result["output_path"] = str(out)
        return result

    async def _op_extract_code_blocks(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        lang_filter = (params.get("language") or "").strip().lower() or None
        blocks = md_utils.code_blocks_with_lines(raw)
        if lang_filter:
            blocks = [b for b in blocks if (b.get("language") or "").strip().lower() == lang_filter]
        return {"success": True, "file": str(path), "code_blocks": blocks, "count": len(blocks)}

    async def _op_build_table(self, params: dict[str, Any]) -> dict[str, Any]:
        headers = params.get("headers")
        rows = params.get("rows") or []
        if not headers:
            raise ValueError("headers is required for build_table")
        table_md = md_utils.build_table_md(headers, rows, params.get("align"))
        result: dict[str, Any] = {
            "success": True,
            "table_markdown": table_md,
            "columns": len(headers),
            "rows": len(rows),
        }
        file_path = params.get("file_path")
        if file_path:
            existing = ""
            try:
                _, existing = self._read_file(file_path)
            except ValueError:
                pass
            sep = "" if not existing or existing.endswith("\n\n") else "\n\n"
            path = self._write_file(file_path, existing + sep + table_md + "\n")
            result["file"] = str(path)
        return result

    async def _op_build_list(self, params: dict[str, Any]) -> dict[str, Any]:
        items = params.get("items")
        if not items:
            raise ValueError("items is required for build_list")
        ordered = bool(params.get("ordered", False))
        list_md = md_utils.build_list_md(items, ordered=ordered)
        result: dict[str, Any] = {
            "success": True,
            "list_markdown": list_md,
            "item_count": len(items),
            "ordered": ordered,
        }
        file_path = params.get("file_path")
        if file_path:
            existing = ""
            try:
                _, existing = self._read_file(file_path)
            except ValueError:
                pass
            sep = "" if not existing or existing.endswith("\n\n") else "\n\n"
            path = self._write_file(file_path, existing + sep + list_md + "\n")
            result["file"] = str(path)
        return result

    async def _op_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        merge_files = params.get("merge_files")
        file_path = params.get("file_path")
        if not merge_files:
            raise ValueError("merge_files is required for merge")
        if not file_path:
            raise ValueError("file_path (output) is required for merge")
        separator = params.get("separator", "\n\n---\n\n")
        parts: list[str] = []
        for fp in merge_files:
            _, text = self._read_file(str(fp))
            parts.append(text.strip())
        path = self._write_file(file_path, separator.join(parts) + "\n")
        return {
            "success": True,
            "file": str(path),
            "files_merged": len(merge_files),
            "chars_written": path.stat().st_size,
        }

    async def _op_format(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        formatted = md_utils.format_markdown(raw)
        self._write_file(str(path), formatted)
        return {
            "success": True,
            "file": str(path),
            "chars_written": len(formatted),
            "original_chars": len(raw),
        }

    async def _op_convert(self, params: dict[str, Any]) -> dict[str, Any]:
        path, raw = self._read_file(params.get("file_path", ""))
        fmt = params.get("output_format", "")
        if fmt not in ("html", "plain_text"):
            raise ValueError("output_format must be html or plain_text")
        content = (
            md_utils.markdown_to_html(raw) if fmt == "html" else md_utils.markdown_to_plain(raw)
        )
        result: dict[str, Any] = {
            "success": True,
            "file": str(path),
            "content": content,
            "output_format": fmt,
        }
        output_path = params.get("output_path")
        if output_path:
            out = self._write_file(output_path, content)
            result["output_path"] = str(out)
            result["written"] = True
        return result

    async def _op_template(self, params: dict[str, Any]) -> dict[str, Any]:
        template_name = params.get("template_name", "")
        if template_name not in md_utils._TEMPLATES:
            raise ValueError(
                f"unknown template: {template_name}. Available: {list(md_utils._TEMPLATES)}"
            )
        doc = md_utils._render_template(template_name, params)
        result: dict[str, Any] = {"success": True, "template": template_name, "content": doc}
        file_path = params.get("file_path")
        if file_path:
            path = self._write_file(file_path, doc)
            result["file"] = str(path)
            result["chars_written"] = path.stat().st_size
        return result


# ── 文档渲染（create/template 共用） ───────────────────────────────
