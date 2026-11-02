# 功能：SlidesGenerateTool — slides 对象数组渲染为幻灯片文件（markdown/html/pptx/pdf）
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.gen.render import format_from_path, render_slides

_FORMAT_EXTS = {
    "markdown": ".md",
    "html": ".html",
    "pptx": ".pptx",
    "pdf": ".pdf",
}


class SlidesGenerateTool(FsTool):
    name: str = "slides_generate"
    description: str = (
        "Render an array of slide objects into a deck file (markdown/html/pptx/pdf). "
        "Each slide has optional `title`, `body` (newline-separated bullet lines) and "
        "`takeaway`. Markdown slides are `---` separated; PPTX uses python-pptx; "
        "PDF uses embedded CJK fonts."
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
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "takeaway": {"type": "string"},
                    },
                },
                "description": "Slide objects; each needs a title, body, or takeaway.",
            },
            "output_path": {
                "type": "string",
                "description": "Destination file path (extension implies format when `format` omitted).",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "html", "pptx", "pdf"],
                "description": "Output format. Default: inferred from output_path extension.",
            },
        },
        "required": ["slides", "output_path"],
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
            data = render_slides(params.get("slides") or [], fmt)
            path.parent.mkdir(parents=True, exist_ok=True)
            if fmt in ("pptx", "pdf"):
                path.write_bytes(data)
            else:
                path.write_text(data.decode("utf-8"), encoding="utf-8")
            return {
                "success": True,
                "file": str(path),
                "format": fmt,
                "slides": len(params.get("slides") or []),
                "size_bytes": path.stat().st_size,
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}
