# 功能：ChecklistGenerateTool — 带状态/优先级/负责人/分组的清单渲染为 markdown/html/docx/pdf
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.gen.document_tool import _FORMAT_EXTS
from bandi.tools.gen.render import format_from_path, render_document

_STATUSES = ("pending", "in_progress", "completed", "blocked", "skipped")
_PRIORITIES = ("low", "medium", "high", "critical")
_CHECK_MAP = {
    "completed": "[x]",
    "in_progress": "[-]",
    "pending": "[ ]",
    "blocked": "[!]",
    "skipped": "[~]",
}
_STATUS_LABEL = {
    "completed": "已完成",
    "in_progress": "进行中",
    "pending": "待办",
    "blocked": "阻塞",
    "skipped": "跳过",
}


class ChecklistGenerateTool(FsTool):
    name: str = "checklist_generate"
    description: str = (
        "Render a status-tracked checklist into a file (markdown/html/docx/pdf). "
        "Each item has `text` and optional `status`, `priority`, `assignee`, "
        "`due_date`, `notes` and `group`. Output includes a progress summary and "
        "per-item checkbox markers. PDF uses embedded CJK fonts."
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
            "title": {"type": "string", "description": "Checklist title (default 'Checklist')."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": list(_STATUSES)},
                        "priority": {"type": "string", "enum": list(_PRIORITIES)},
                        "assignee": {"type": "string"},
                        "due_date": {"type": "string"},
                        "notes": {"type": "string"},
                        "group": {"type": "string"},
                    },
                    "required": ["text"],
                },
                "description": "Checklist items.",
            },
            "group_by": {
                "type": "string",
                "description": "Group items by this item field ('group' or 'status').",
            },
            "output_path": {"type": "string", "description": "Destination file path."},
            "format": {
                "type": "string",
                "enum": ["markdown", "html", "docx", "pdf"],
                "description": "Output format. Default: inferred from extension.",
            },
        },
        "required": ["items", "output_path"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            items = params.get("items") or []
            if not items:
                return {"success": False, "error": "items must contain at least one item"}
            md = _build_checklist_markdown(params)
            path = self._resolve_output(params.get("output_path", ""), context)
            fmt = params.get("format") or format_from_path(path, _FORMAT_EXTS)
            if fmt is None:
                return {
                    "success": False,
                    "error": (
                        "format is required when output_path has no recognized extension "
                        "(pdf/docx/html/md)"
                    ),
                }
            data = render_document(md, fmt)
            path.parent.mkdir(parents=True, exist_ok=True)
            if fmt in ("docx", "pdf"):
                path.write_bytes(data)
            else:
                path.write_text(data.decode("utf-8"), encoding="utf-8")
            completed = sum(1 for it in items if it.get("status") == "completed")
            return {
                "success": True,
                "file": str(path),
                "format": fmt,
                "items": len(items),
                "completed": completed,
                "total": len(items),
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}


def _build_checklist_markdown(params: dict[str, Any]) -> str:
    title = (params.get("title") or "Checklist").strip()
    items = params.get("items") or []
    lines: list[str] = [f"# {title}", ""]
    completed = sum(1 for it in items if it.get("status") == "completed")
    total = len(items)
    pct = round(100 * completed / total) if total else 0
    lines.append(f"## 进度：{completed}/{total} 已完成（{pct}%）")
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_by = params.get("group_by")
    if group_by == "status":
        for it in items:
            grouped.setdefault(str(it.get("status") or "pending"), []).append(it)
    elif group_by == "group":
        for it in items:
            grouped.setdefault(str(it.get("group") or "其他"), []).append(it)
    else:
        grouped["全部"] = items
    for group, group_items in grouped.items():
        lines.append("")
        lines.append(f"### {group}")
        for it in group_items:
            marker = _CHECK_MAP.get(str(it.get("status") or "pending"), "[ ]")
            label = _STATUS_LABEL.get(str(it.get("status") or "pending"), "")
            tags: list[str] = []
            if it.get("priority"):
                tags.append(f"[{it['priority']}]")
            if it.get("assignee"):
                tags.append(f"@{it['assignee']}")
            if it.get("due_date"):
                tags.append(f"{it['due_date']}")
            suffix = f"（{label}）" + (f" {', '.join(tags)}" if tags else "")
            line = f"- {marker} {it.get('text', '')}{suffix}"
            if it.get("notes"):
                line += f" — 备注：{it['notes']}"
            lines.append(line)
    return "\n".join(lines) + "\n"
