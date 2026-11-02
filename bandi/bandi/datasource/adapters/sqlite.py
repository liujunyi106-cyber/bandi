from __future__ import annotations

import sqlite3

from bandi.datasource.adapters.base_sql import BaseSqlAdapter
from bandi.datasource.core import (
    AdapterExecutionInput,
    AdapterPreviewInput,
    AdapterSqlInput,
    ColumnSchema,
    SchemaSummary,
    TableResult,
    TableSchema,
    _adapter_guard,
)
from bandi.utils.db import connect


class SqliteAdapter(BaseSqlAdapter):
    conn_type = "sqlite"

    def __init__(self, config: dict) -> None:
        self._config = config

    def _connect(self) -> sqlite3.Connection:
        path = self._config.get("path", ":memory:")
        # 外部/用户数据源：只读 + 写等待，**绝不开启 WAL**——WAL 是写进库文件头部的
        # 持久化设置，对外部库开启等于擅自修改用户文件（契约 ）。
        return connect(path, row_factory=True, query_only=True)

    @_adapter_guard
    async def inspect_schema(
        self, input: AdapterExecutionInput = AdapterExecutionInput()
    ) -> SchemaSummary:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            table_names = [r["name"] for r in cursor.fetchall()]
            tables = []
            for tname in table_names:
                cursor = conn.execute(f'PRAGMA table_info("{tname}")')
                cols = []
                for r in cursor.fetchall():
                    cols.append(
                        ColumnSchema(
                            name=r["name"],
                            type=r["type"] or "TEXT",
                            nullable=not r["notnull"],
                            primary_key=bool(r["pk"]),
                        )
                    )
                tables.append(TableSchema(name=tname, columns=cols))
            return SchemaSummary(tables=tables)
        finally:
            conn.close()

    @_adapter_guard
    async def preview_table(self, input: AdapterPreviewInput) -> TableResult:
        conn = self._connect()
        try:
            self._assert_table_allowed(conn, input.table)
            cursor = conn.execute(f'SELECT * FROM "{input.table}" LIMIT ?', (input.limit,))
            return self._cursor_to_result(cursor)
        finally:
            conn.close()

    @_adapter_guard
    async def run_sql_readonly(self, input: AdapterSqlInput) -> TableResult:
        self._assert_readonly(input.sql)
        conn = self._connect()
        try:
            cursor = conn.execute(self._apply_limit(input.sql, input.limit))
            return self._cursor_to_result(cursor)
        finally:
            conn.close()

    def _assert_table_allowed(self, conn: sqlite3.Connection, table: str) -> None:
        # 功能：表名白名单校验——仅允许真实存在于库中的表，防表名注入
        # 含双引号表名显式拒绝（`FROM "{table}"` 拼接无法转义引号表名）
        if '"' in table:
            raise ValueError(f"unknown table: {table!r}")
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in cursor.fetchall()}
        if table not in names:
            raise ValueError(f"unknown table: {table!r}")

    def _cursor_to_result(self, cursor: sqlite3.Cursor) -> TableResult:
        cols = [d[0] for d in cursor.description] if cursor.description else []
        rows = [list(r) for r in cursor.fetchall()]
        return TableResult(columns=cols, rows=rows, row_count=len(rows))
