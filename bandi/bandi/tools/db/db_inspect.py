# 功能：db_inspect——沙箱内数据库内省（list_tables / describe_table 列·主键·外键）
from __future__ import annotations

import sqlite3
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.db import _db_io


class DbInspectTool(BaseTool):
    name = "db_inspect"
    description = "内省沙箱内 SQLite 库：list_tables 列出全部用户表；describe_table 返回列（名称/类型/是否可空/主键序号）、复合主键与外键引用。"
    category = ToolCategory.DATA
    feature_group = "db"
    capability_tags = [ToolCapability.DATABASE.value]
    risk_level = "low"
    is_read_only = True
    is_idempotent = True
    aliases = ("db_schema", "list_db")
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list_tables", "describe_table"],
                "default": "list_tables",
            },
            "db_path": {
                "type": "string",
                "description": "目标库文件的绝对路径（须位于数据沙箱内）。",
            },
            "table_name": {"type": "string", "description": "describe_table 要描述的表名。"},
        },
        "required": ["db_path"],
    }

    def __init__(self, data_root: str | None = None) -> None:
        self.data_root = data_root or str(_db_io.DEFAULT_DATA_ROOT)

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        try:
            op = params.get("operation") or "list_tables"
            if op == "list_tables":
                return self._list_tables(params["db_path"])
            if op == "describe_table":
                table = params.get("table_name")
                if not table or not isinstance(table, str):
                    raise ValueError("'table_name' is required for describe_table")
                return self._describe_table(params["db_path"], table)
            raise ValueError(f"unknown operation: {op}")
        except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error, OSError) as e:
            return _db_io.fail(str(e))

    def _list_tables(self, db_path: str) -> dict[str, Any]:
        conn = _db_io.connect(db_path, self.data_root, 30)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = [row["name"] for row in rows]
            return _db_io.ok(tables=tables, count=len(tables))
        finally:
            conn.close()

    def _describe_table(self, db_path: str, table: str) -> dict[str, Any]:
        if '"' in table:
            raise ValueError(f"invalid table name: {table!r}")
        conn = _db_io.connect(db_path, self.data_root, 30)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"table not found: {table}")
            cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            fks = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            columns = [
                {
                    "name": row["name"],
                    "type": row["type"],
                    "notnull": row["notnull"],
                    "pk": row["pk"],
                }
                for row in cols
            ]
            primary_key = [
                row["name"] for row in sorted(cols, key=lambda r: r["pk"]) if row["pk"] > 0
            ]
            foreign_keys = [
                {"from": row["from"], "table": row["table"], "to": row["to"]} for row in fks
            ]
            return _db_io.ok(
                table=table,
                columns=columns,
                primary_key=primary_key,
                foreign_keys=foreign_keys,
            )
        finally:
            conn.close()
