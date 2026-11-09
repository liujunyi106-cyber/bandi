# 功能：CSVProcessorTool — CSV/处理（read/query/write/stats/convert + 分隔符嗅探）

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import _MAX_FILE_SIZE, FsTool

_READ_ROW_CAP = 10_000
_UNIQUE_CAP = 50_000
_SNIFF_BYTES = 65_536
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
)


class CSVProcessorTool(FsTool):
    name: str = "csv_processor"
    # 安全（③⁻）：CSV 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Read, query, write, analyze, and convert CSV/TSV files with delimiter sniffing "
        "and encoding detection. Operations: read (rows up to 10000), query (filter by "
        "column value, optional sort), write (dict rows to file), stats (column profiles), "
        "convert (CSV to JSON file)."
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
                "enum": ["read", "query", "write", "stats", "convert"],
                "description": "Operation to perform",
            },
            "file_path": {
                "type": "string",
                "description": "CSV/TSV path: existing file for read/query/stats/convert; destination for write",
            },
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "delimiter": {"type": "string", "description": "Field delimiter (default auto-detect)"},
            "encoding": {
                "type": "string",
                "description": "Text encoding (default utf-8 / latin-1 fallback)",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "description": "Max data rows for read (cap 10000)",
            },
            "has_header": {
                "type": "boolean",
                "description": "First row is header (read/query/stats/convert). Default true",
            },
            "filter_column": {"type": "string", "description": "Column to filter on (query)"},
            "filter_value": {"type": "string", "description": "Value to match (query)"},
            "sort_column": {"type": "string", "description": "Column to sort by (query)"},
            "sort_order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort order (query, default asc)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Max rows after filter/sort (query)",
            },
            "data": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Array of row objects (write)",
            },
            "output_path": {"type": "string", "description": "Output file path (convert)"},
            "output_format": {
                "type": "string",
                "enum": ["json"],
                "description": "Output format for convert. Default json",
            },
        },
        "required": ["operation"],
    }
    is_concurrency_safe: bool = False
    # medium 而非 low——与 apply_patch/file_edit 对齐
    risk_level: str = "medium"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        dispatch = {
            "read": self._op_read,
            "query": self._op_query,
            "write": self._op_write,
            "stats": self._op_stats,
            "convert": self._op_convert,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}

    # ── 解析辅助 ────────────────────────────────────────────────────────

    def _resolve_input(self, path_str: str):
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if not path.is_file():
            raise ValueError(f"file not found: {path_str}")
        return path

    @staticmethod
    def _detect_encoding(raw: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                raw.decode(enc)
                return "utf-8-sig" if enc == "utf-8-sig" else enc
            except UnicodeDecodeError:
                continue
        return "latin-1"

    @staticmethod
    def _sniff_delimiter(sample: str) -> str:
        try:
            return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            if "\t" in sample:
                return "\t"
            return ","

    def _read_file(
        self, path: Path, delimiter: str | None = None, encoding: str | None = None
    ) -> tuple[list[list[str]], str, str]:
        if path.stat().st_size > _MAX_FILE_SIZE:
            raise ValueError(f"file exceeds max size ({_MAX_FILE_SIZE} bytes)")
        raw = path.read_bytes()
        enc = encoding or self._detect_encoding(raw)
        text = raw.decode(enc, errors="replace")
        sample = text[:_SNIFF_BYTES]
        delim = delimiter or self._sniff_delimiter(sample)
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
        rows = list(reader)
        return rows, enc, delim

    @staticmethod
    def _normalize_cell(value: Any) -> str:
        return "" if value is None else str(value).strip()

    @staticmethod
    def _try_float(s: str) -> float | None:
        if not s:
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _try_date(s: str) -> datetime | None:
        if not s:
            return None
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _build_headers_rows(self, path: Path, params: dict[str, Any]):
        rows, enc, delim = self._read_file(path, params.get("delimiter"), params.get("encoding"))
        if not rows:
            return [], [], enc, delim
        has_header = params.get("has_header", True)
        if has_header:
            headers = [h if h else f"column_{i}" for i, h in enumerate(rows[0])]
            data_rows = rows[1:]
        else:
            headers = [f"column_{i}" for i in range(len(rows[0]))]
            data_rows = rows
        return headers, data_rows, enc, delim

    @staticmethod
    def _row_to_dict(row: list[str], headers: list[str]) -> dict[str, Any]:
        return {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}

    # ── 操作实现 ────────────────────────────────────────────────────────

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_input(params.get("file_path", ""))
        headers, data_rows, enc, delim = self._build_headers_rows(path, params)
        cap = min(params.get("max_rows", _READ_ROW_CAP), _READ_ROW_CAP)
        rows = [self._row_to_dict(r, headers) for r in data_rows[:cap]]
        return {
            "success": True,
            "headers": headers,
            "rows": rows,
            # （rows 被 cap 截断但计数未截断，下游按 row_count 遍历越界误解）
            "row_count": len(rows),
            "column_count": len(headers),
            "encoding": enc,
            "delimiter": delim,
        }

    async def _op_query(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_input(params.get("file_path", ""))
        filter_column = params.get("filter_column", "")
        filter_value = params.get("filter_value", "")
        if not filter_column or not filter_value:
            raise ValueError("query requires filter_column and filter_value")
        headers, data_rows, enc, delim = self._build_headers_rows(path, params)
        if filter_column not in headers:
            raise ValueError(f"unknown column: {filter_column}. Available: {headers}")
        col_idx = headers.index(filter_column)
        matched = [
            r
            for r in data_rows
            if self._normalize_cell(r[col_idx] if col_idx < len(r) else "") == filter_value
        ]

        sort_column = params.get("sort_column")
        if sort_column is not None and sort_column not in headers:
            raise ValueError(f"unknown sort column: {sort_column}")
        if sort_column:
            sort_idx = headers.index(sort_column)
            reverse = params.get("sort_order", "asc") == "desc"

            def sort_key(row: list[str]) -> tuple[int, float | str]:
                val = self._normalize_cell(row[sort_idx] if sort_idx < len(row) else "")
                num = self._try_float(val)
                if num is not None:
                    # 排序反（-1.5 排在 -2.0 前）
                    return (0, num)
                return (1, val)

            matched.sort(key=sort_key, reverse=reverse)

        max_out = params.get("limit", 0) or 0
        if max_out > 0:
            matched = matched[:max_out]
        rows = [self._row_to_dict(r, headers) for r in matched]
        return {
            "success": True,
            "rows": rows,
            "row_count": len(matched),
            "encoding": enc,
            "delimiter": delim,
        }

    async def _op_write(self, params: dict[str, Any]) -> dict[str, Any]:
        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("file_path is required for write")
        data = params.get("data")
        if not isinstance(data, list) or not data or not all(isinstance(d, dict) for d in data):
            raise ValueError("data must be a non-empty list of row objects")
        path = self._resolve_safe_path(file_path)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {file_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        keys: list[str] = []
        for d in data:
            for k in d:
                if k not in keys:
                    keys.append(k)
        if not keys:
            raise ValueError("data rows have no keys")
        delim = params.get("delimiter", ",")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys, delimiter=delim, extrasaction="ignore")
            writer.writeheader()
            for d in data:
                writer.writerow({k: d.get(k) for k in keys})
        return {"success": True, "row_count": len(data), "path": str(path)}

    async def _op_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_input(params.get("file_path", ""))
        headers, data_rows, enc, delim = self._build_headers_rows(path, params)
        columns: dict[str, dict[str, Any]] = {}
        for i, h in enumerate(headers):
            null_count = 0
            unique: set[str] = set()
            unique_truncated = False
            samples: list[str] = []
            numeric_vals: list[float] = []
            date_hits = 0
            non_empty = 0
            for row in data_rows:
                val = self._normalize_cell(row[i] if i < len(row) else "")
                if val == "":
                    null_count += 1
                    continue
                non_empty += 1
                if len(unique) < _UNIQUE_CAP:
                    unique.add(val)
                else:
                    unique_truncated = True
                if len(samples) < 5 and val not in samples:
                    samples.append(val)
                num = self._try_float(val)
                if num is not None:
                    numeric_vals.append(num)
                if self._try_date(val) is not None:
                    date_hits += 1
            if non_empty == 0:
                inferred = "string"
                mn = mx = None
            elif len(numeric_vals) == non_empty:
                inferred = "numeric"
                mn = min(numeric_vals)
                mx = max(numeric_vals)
            elif date_hits >= non_empty * 0.8:
                inferred = "date"
                mn = mx = None
            else:
                inferred = "string"
                mn = mx = None
            columns[h] = {
                "inferred_type": inferred,
                "null_count": null_count,
                "unique_count": len(unique),
                "unique_count_truncated": unique_truncated,
                "sample_values": samples,
                "min": mn,
                "max": mx,
            }
        return {
            "success": True,
            "columns": columns,
            "encoding": enc,
            "delimiter": delim,
            "total_rows": len(data_rows),
        }

    async def _op_convert(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_input(params.get("file_path", ""))
        output_path = params.get("output_path", "")
        output_format = params.get("output_format", "json")
        if output_format != "json":
            raise ValueError(f"unsupported output format: {output_format}; only json")
        if not output_path:
            raise ValueError("output_path is required for convert")
        out = self._resolve_safe_path(output_path)
        if self._is_sensitive(out):
            raise ValueError(f"sensitive path rejected: {output_path}")
        out.parent.mkdir(parents=True, exist_ok=True)
        headers, data_rows, _enc, _delim = self._build_headers_rows(path, params)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write("[\n")
            for i, row in enumerate(data_rows):
                rec = self._row_to_dict(row, headers)
                f.write(json.dumps(rec, ensure_ascii=False, indent=2))
                if i < len(data_rows) - 1:
                    f.write(",\n")
                else:
                    f.write("\n")
            f.write("]\n")
        return {"success": True, "path": str(out), "row_count": len(data_rows)}
