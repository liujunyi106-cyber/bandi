# 功能：DocumentGenerateTool — markdown 文档按格式渲染落盘（markdown/html/docx/pdf）
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.gen.render import format_from_path, render_document

_FORMAT_EXTS = {
    "markdown": ".md",
    "html": ".html",
    "docx": ".docx",
    "pdf": ".pdf",
}


class DocumentGenerateTool(FsTool):
    name: str = "document_generate"
    description: str = (
        "Render a markdown document into a file. Accepts markdown `content`, "
        "renders it in the requested `format` (markdown/html/docx/pdf) and writes "
        "it to `output_path`. The markdown subset covers headings, paragraphs, "
        "bullet/numbered lists, code fences, tables, blockquotes and horizontal "
        "rules. PDF output uses embedded CJK fonts; DOCX uses python-docx."
    )
    category: ToolCategory = ToolCategory.GEN
    feature_group: str = "gen"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Markdown content to render.",
            },
            "output_path": {
                "type": "string",
                "description": "Destination file path (extension implies format when `format` omitted).",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "html", "docx", "pdf"],
                "description": "Output format. Default: inferred from output_path extension.",
            },
        },
        "required": ["content", "output_path"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            path = self._resolve_output(params.get("output_path", ""), context)
            fmt = params.get("format") or format_from_path(path, _FORMAT_EXTS)
            if fmt is None:
                return {
                    "success": False,
                    "error": (
                        "format is required when output_path has no recognized extension "
                        f"(one of {sorted(_FORMAT_EXTS)})"
                    ),
                }
            data = render_document(params.get("content", ""), fmt)
            if isinstance(data, str):
                data = data.encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            if _is_binary_fmt(fmt):
                path.write_bytes(data)
            else:
                path.write_text(data.decode("utf-8"), encoding="utf-8")
            return {
                "success": True,
                "file": str(path),
                "format": fmt,
                "size_bytes": path.stat().st_size,
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}


def _is_binary_fmt(fmt: str) -> bool:
    return fmt in ("docx", "pdf")
