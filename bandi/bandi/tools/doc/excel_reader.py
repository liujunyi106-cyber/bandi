# 功能：ExcelReaderTool — Excel 读取（sheet 选择/单元格区域/输出格式三态）

from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool


class ExcelReaderTool(FsTool):
    name: str = "excel_reader"
    # 安全（③⁻）：Excel 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Read Excel files (.xlsx/.xlsm): sheet selection by name or index, optional cell "
        "range (e.g. A1:D10), header handling, and three output formats: dict (column-"
        "oriented), records (list of row dicts), or list (raw 2D)."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the Excel file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "sheet_name": {"type": "string", "description": "Sheet to read (default active)"},
            "sheet_index": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based sheet index (ignored if sheet_name given)",
            },
            "cell_range": {
                "type": "string",
                "description": "Cell range to extract, e.g. A1:D10 (default all data)",
            },
            "has_header": {
                "type": "boolean",
                "description": "Whether the first row contains headers. Default true",
            },
            "output_format": {
                "type": "string",
                "enum": ["dict", "records", "list"],
                "description": "Output format. Default records",
            },
            "include_empty_cells": {
                "type": "boolean",
                "description": "Whether to include empty cells as None. Default true",
            },
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file():
                return {"success": False, "error": f"file not found: {path_str}"}
            if path.suffix.lower() not in (".xlsx", ".xlsm"):
                return {
                    "success": False,
                    "error": f"unsupported Excel format; convert to .xlsx: {path.suffix}",
                }
            return self._read_excel(path, params)
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _read_excel(self, path, params: dict[str, Any]) -> dict[str, Any]:
        try:
            import openpyxl
            from openpyxl.utils import range_boundaries
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError:
            raise RuntimeError("openpyxl is required for Excel reading; install openpyxl")
        try:
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        except InvalidFileException:
            raise ValueError(f"invalid or corrupted xlsx: {path}")
        try:
            sheet_names = list(wb.sheetnames)
            sheet_name = params.get("sheet_name")
            if sheet_name:
                if sheet_name not in wb.sheetnames:
                    raise ValueError(f"sheet not found: {sheet_name}. Available: {sheet_names}")
                ws = wb[sheet_name]
            else:
                idx = params.get("sheet_index", 0)
                if idx < 0 or idx >= len(wb.sheetnames):
                    raise ValueError(f"sheet index out of range (0-{len(wb.sheetnames) - 1})")
                sheet_name = wb.sheetnames[idx]
                ws = wb[sheet_name]

            cell_range = params.get("cell_range")
            if cell_range:
                min_col, min_row, max_col, max_row = range_boundaries(cell_range)
                rows_iter = ws.iter_rows(
                    min_row=min_row,
                    max_row=max_row,
                    min_col=min_col,
                    max_col=max_col,
                    values_only=True,
                )
            else:
                rows_iter = ws.iter_rows(values_only=True)

            raw_data: list[list[Any]] = []
            for row in rows_iter:
                raw_data.append(list(row))
        finally:
            wb.close()

        if not raw_data:
            return {
                "success": True,
                "data": [],
                "sheet_name": sheet_name,
                "sheet_names": sheet_names,
                "row_count": 0,
                "column_count": 0,
                "output_format": params.get("output_format", "records"),
            }

        include_empty = params.get("include_empty_cells", True)
        # include_empty_cells=False 不得改变列结构（整行过滤会使后续列前移错位）：
        # 单元格一律保留位置，空值仅在 records/dict 输出阶段按格式省略
        normalized = [[self._cell_to_json(v) for v in row] for row in raw_data]

        has_header = params.get("has_header", True)
        if has_header:
            headers = [
                (str(h).strip() if h is not None and str(h).strip() else f"Column_{i + 1}")
                for i, h in enumerate(normalized[0])
            ]
            data_rows = normalized[1:]
        else:
            headers = [f"Column_{i + 1}" for i in range(len(normalized[0]))]
            data_rows = normalized

        output_format = params.get("output_format", "records")
        if output_format == "dict":
            data: Any = {}
            for i, h in enumerate(headers):
                col = [row[i] if i < len(row) else None for row in data_rows if row is not None]
                data[h] = col if include_empty else [v for v in col if v is not None]
        elif output_format == "list":
            data = [headers] + data_rows
        else:
            data = [
                {
                    h: row[i]
                    for i, h in enumerate(headers)
                    if i < len(row) and (include_empty or row[i] is not None)
                }
                for row in data_rows
            ]

        result: dict[str, Any] = {
            "success": True,
            "data": data,
            "headers": headers,
            "sheet_name": sheet_name,
            "sheet_names": sheet_names,
            "row_count": len(data_rows),
            "column_count": len(headers),
            "output_format": output_format,
        }
        if cell_range:
            result["cell_range"] = cell_range
        return result

    @staticmethod
    def _cell_to_json(value: object) -> str | int | float | bool | None:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except (ValueError, TypeError):
                return str(value)
        return str(value)
