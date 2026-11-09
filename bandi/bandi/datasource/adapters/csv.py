from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

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

# 严格数字形态：拒绝 Python int()/float() 的宽松特例（`1_0` 下划线、`nan`/`inf`）
_INT_RE = re.compile(r"[+-]?\d+")
_FLOAT_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_SELECT_RE = re.compile(
    r'^\s*SELECT\s+(.+?)\s+FROM\s+([`"\']?)([^\s`"\']+)\2(?:\s+LIMIT\s+(\d+))?\s*$',
    re.IGNORECASE,
)


class CsvAdapter(DataSourceAdapter):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._dir = Path(config.get("path", ".")).expanduser()
        # 编码可配置（GBK 中文 CSV）
        self._encoding = str(config.get("encoding", "utf-8-sig"))

    def _list_csv_files(self) -> list[Path]:
        if self._dir.is_file() and self._dir.suffix.lower() == ".csv":
            return [self._dir]
        pattern = self._config.get("pattern", "*.csv")
        return sorted(self._dir.glob(pattern))

    def _read_csv(self, filepath: Path, limit: int = 0) -> tuple[list[str], list[list[Any]], int]:
        with open(filepath, newline="", encoding=self._encoding) as f:
            reader = csv.reader(f)
            try:
                headers = next(reader)
            except StopIteration:
                return [], [], 0
            rows: list[list[Any]] = []
            total = 0
            for row in reader:
                total += 1
                if limit == 0 or len(rows) < limit:
                    rows.append(row)
                elif limit > 0:
                    break
            return headers, rows, total

    def _infer_types(self, filepath: Path, sample_rows: list[list[Any]]) -> list[str]:
        if not sample_rows or not sample_rows[0]:
            return ["TEXT"] * (len(sample_rows[0]) if sample_rows else 0)
        types: list[str] = []
        for col_idx in range(len(sample_rows[0])):
            col_vals: list[str] = []
            for row in sample_rows:
                if col_idx < len(row) and isinstance(row[col_idx], str) and row[col_idx].strip():
                    col_vals.append(row[col_idx])
            # 空列判 TEXT（`all([])=True` 会把空列误判为 INTEGER）
            if col_vals and all(self._is_int(v) for v in col_vals):
                types.append("INTEGER")
            elif col_vals and all(self._is_float(v) for v in col_vals):
                types.append("FLOAT")
            else:
                types.append("TEXT")
        return types

    def _is_int(self, v: str) -> bool:
        return bool(_INT_RE.fullmatch(v.strip()))

    def _is_float(self, v: str) -> bool:
        return bool(_FLOAT_RE.fullmatch(v.strip()))

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        tables = []
        for fp in self._list_csv_files():
            headers, sample_rows, _ = self._read_csv(fp, limit=100)
            types = self._infer_types(fp, sample_rows)
            cols = []
            for i, h in enumerate(headers):
                col_type = types[i] if types and i < len(types) else "TEXT"
                cols.append(ColumnSchema(name=h, type=col_type))
            tables.append(TableSchema(name=fp.stem, columns=cols))
        return SchemaSummary(tables=tables)

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        candidates = [p for p in self._list_csv_files() if p.stem == input.table]
        if not candidates:
            return TableResult()
        headers, rows, _ = self._read_csv(candidates[0], limit=input.limit)
        return TableResult(columns=headers, rows=rows, row_count=len(rows))

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        match = _SELECT_RE.match(input.sql)
        if not match:
            raise ValueError(f"CSV adapter only supports simple SELECT: {input.sql}")
        raw_columns = match.group(1).strip()
        table_name = match.group(3)
        limit = int(match.group(4)) if match.group(4) else input.limit
        candidates = [p for p in self._list_csv_files() if p.stem == table_name]
        if not candidates:
            return TableResult()
        headers, rows, _ = self._read_csv(candidates[0], limit=limit)
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
