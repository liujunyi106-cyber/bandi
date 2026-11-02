from __future__ import annotations

import re
from pathlib import Path

from bandi.datasource.core import (
    AdapterExecutionInput,
    AdapterPreviewInput,
    AdapterSqlInput,
    ColumnSchema,
    DataSourceAdapter,
    SchemaSummary,
    TableResult,
    TableSchema,
    _adapter_guard,
)

_SELECT_RE = re.compile(
    r'^\s*SELECT\s+(.+?)\s+FROM\s+([`"\']?)([^\s`"\']+)\2(?:\s+LIMIT\s+(\d+))?\s*$',
    re.IGNORECASE,
)


class XlsxAdapter(DataSourceAdapter):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._path = Path(config.get("path", ".")).expanduser()

    def _open(self):
        try:
            import openpyxl
        except ImportError:
            raise ImportError("openpyxl is required for Excel adapter")
        return openpyxl.load_workbook(self._path, read_only=True, data_only=True)

    def _read_sheet(self, wb, sheet_name: str, limit: int = 0) -> tuple[list[str], list[list], int]:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = [str(h) if h is not None else "" for h in next(rows_iter)]
        except StopIteration:
            return [], [], 0
        rows: list[list] = []
        total = 0
        for row in rows_iter:
            total += 1
            if limit == 0 or len(rows) < limit:
                rows.append(list(row))
        return headers, rows, total

    def _infer_sheet_types(self, sample_rows: list[list]) -> list[str]:
        # 功能：按样本值的 Python 类型推断列类型（INTEGER//TEXT；bool 不视作数字）
        if not sample_rows:
            return []
        width = max(len(r) for r in sample_rows)
        types: list[str] = []
        for idx in range(width):
            values = [r[idx] for r in sample_rows if idx < len(r) and r[idx] is not None]
            if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                types.append("INTEGER")
            elif values and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
            ):
                types.append("FLOAT")
            else:
                types.append("TEXT")
        return types

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        wb = self._open()
        try:
            tables = []
            for name in wb.sheetnames:
                headers, sample, _ = self._read_sheet(wb, name, limit=100)
                types = self._infer_sheet_types(sample)
                cols = [
                    ColumnSchema(name=h, type=types[i] if i < len(types) else "TEXT")
                    for i, h in enumerate(headers)
                ]
                tables.append(TableSchema(name=name, columns=cols))
            return SchemaSummary(tables=tables)
        finally:
            wb.close()

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        # 缺 sheet 返回空结果（与 csv/parquet 契约一致）
        wb = self._open()
        try:
            if input.table not in wb.sheetnames:
                return TableResult()
            headers, rows, _ = self._read_sheet(wb, input.table, limit=input.limit)
            return TableResult(columns=headers, rows=rows, row_count=len(rows))
        finally:
            wb.close()

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        match = _SELECT_RE.match(input.sql)
        if not match:
            raise ValueError(f"Excel adapter only supports simple SELECT: {input.sql}")
        raw_columns = match.group(1).strip()
        table_name = match.group(3)
        limit = int(match.group(4)) if match.group(4) else input.limit
        wb = self._open()
        try:
            if table_name not in wb.sheetnames:
                return TableResult()
            headers, rows, _ = self._read_sheet(wb, table_name, limit=limit)
            limited = rows[: min(limit, len(rows))]
            if raw_columns == "*":
                return TableResult(columns=headers, rows=limited, row_count=len(limited))
            selected = [c.strip().strip("`\"'") for c in raw_columns.split(",")]
            unknown = [c for c in selected if c not in headers]
            if unknown:
                raise ValueError(f"unknown column(s): {', '.join(unknown)} (table {table_name})")
            col_indices = [headers.index(c) for c in selected]
            filtered = [[row[i] for i in col_indices] for row in limited]
            return TableResult(
                columns=[headers[i] for i in col_indices], rows=filtered, row_count=len(filtered)
            )
        finally:
            wb.close()
