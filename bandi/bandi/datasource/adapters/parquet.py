from __future__ import annotations

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

_SELECT_RE = re.compile(
    r'^\s*SELECT\s+(.+?)\s+FROM\s+([`"\']?)([^\s`"\']+)\2(?:\s+LIMIT\s+(\d+))?\s*$',
    re.IGNORECASE,
)


class ParquetAdapter(DataSourceAdapter):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._dir = Path(config.get("path", ".")).expanduser()

    def _list_parquet_files(self) -> list[Path]:
        if self._dir.is_file() and self._dir.suffix.lower() in (".parquet", ".pq"):
            return [self._dir]
        pattern = self._config.get("pattern", "*.parquet")
        return sorted(self._dir.glob(pattern))

    def _read_parquet_metadata(self, filepath: Path) -> tuple[list[str], list[str], int]:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required for ParquetAdapter: pip install pyarrow")

        pf = pq.ParquetFile(filepath)
        try:
            schema = pf.schema_arrow
            columns = [schema.field(i).name for i in range(len(schema))]
            types = [str(schema.field(i).type) for i in range(len(schema))]
            return columns, types, pf.metadata.num_rows
        finally:
            pf.close()

    def _read_parquet_rows(self, filepath: Path, limit: int) -> list[list[Any]]:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required for ParquetAdapter: pip install pyarrow")

        if limit <= 0:
            return []
        pf = pq.ParquetFile(filepath)
        try:
            batch = next(pf.iter_batches(batch_size=limit), None)
            if batch is None:
                return []
            # 用 to_pylist 读取以原生保留 None（to_pandas 会把 null 失真为 NaN/float64）
            names = batch.column_names
            return [[rec.get(n) for n in names] for rec in batch.to_pylist()[:limit]]
        finally:
            pf.close()

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        tables = []
        for fp in self._list_parquet_files():
            columns, types, _ = self._read_parquet_metadata(fp)
            cols = [ColumnSchema(name=c, type=t) for c, t in zip(columns, types)]
            tables.append(TableSchema(name=fp.stem, columns=cols))
        return SchemaSummary(tables=tables)

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        candidates = [p for p in self._list_parquet_files() if p.stem == input.table]
        if not candidates:
            return TableResult()
        columns, _types, _ = self._read_parquet_metadata(candidates[0])
        rows = self._read_parquet_rows(candidates[0], input.limit)
        return TableResult(columns=columns, rows=rows, row_count=len(rows))

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        match = _SELECT_RE.match(input.sql)
        if not match:
            raise ValueError(f"Parquet adapter only supports simple SELECT: {input.sql}")
        raw_columns = match.group(1).strip()
        table_name = match.group(3)
        limit = int(match.group(4)) if match.group(4) else input.limit
        candidates = [p for p in self._list_parquet_files() if p.stem == table_name]
        if not candidates:
            return TableResult()
        columns, _types, _ = self._read_parquet_metadata(candidates[0])
        rows = self._read_parquet_rows(candidates[0], limit)
        if raw_columns == "*":
            return TableResult(columns=columns, rows=rows, row_count=len(rows))
        selected = [c.strip().strip("`\"'") for c in raw_columns.split(",")]
        unknown = [c for c in selected if c not in columns]
        if unknown:
            raise ValueError(f"unknown column(s): {', '.join(unknown)} (table {table_name})")
        col_indices = [columns.index(c) for c in selected]
        filtered = [[row[i] for i in col_indices] for row in rows]
        return TableResult(
            columns=[columns[i] for i in col_indices], rows=filtered, row_count=len(filtered)
        )
