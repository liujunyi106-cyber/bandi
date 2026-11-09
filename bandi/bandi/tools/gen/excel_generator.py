# 功能：ExcelGenerateTool — 多 sheet Excel 生成（表头/批量行/冻结首行）
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool


class ExcelGenerateTool(FsTool):
    name: str = "excel_generate"
    description: str = (
        "Create an Excel workbook (.xlsx). Accepts a list of `sheets`, each with "
        "optional `name`, optional `headers` (header row) and required `rows` "
        "(list of row lists, each cell a scalar). Freezes the header row when "
        "present. Writes the workbook to `output_path`."
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
            "sheets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Sheet name (default Sheet1/2/...).",
                        },
                        "headers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional header row.",
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "number"},
                                        {"type": "boolean"},
                                        {"type": "null"},
                                    ]
                                },
                            },
                            "description": "Data rows (scalar cells).",
                        },
                    },
                    "required": ["rows"],
                },
                "description": "Workbook sheets.",
            },
            "output_path": {"type": "string", "description": "Destination .xlsx path."},
        },
        "required": ["sheets", "output_path"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            import openpyxl

            sheets = params.get("sheets") or []
            if not sheets:
                raise ValueError("sheets must contain at least one sheet")
            path = self._resolve_output(params.get("output_path", ""), context)
            wb = openpyxl.Workbook()
            first = True
            sheet_count = 0
            for index, sheet in enumerate(sheets):
                if first:
                    ws = wb.active
                    first = False
                else:
                    ws = wb.create_sheet()
                name = (sheet.get("name") or f"Sheet{index + 1}").strip()
                if name:
                    ws.title = name
                headers = sheet.get("headers") or []
                rows = sheet.get("rows") or []
                if headers:
                    ws.append([_cell_value(h) for h in headers])
                for row in rows:
                    ws.append([_cell_value(c) for c in (row or [])])
                if headers:
                    ws.freeze_panes = "A2"
                sheet_count += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            wb.save(str(path))
            return {
                "success": True,
                "file": str(path),
                "sheets": sheet_count,
                "size_bytes": path.stat().st_size,
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}


def _cell_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
