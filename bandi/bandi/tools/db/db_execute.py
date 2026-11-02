# 功能：db_execute——沙箱内单语句写操作（DDL/DML），破坏性 SQL 需 confirm_destructive，禁 ATTACH，命名参数绑定
from __future__ import annotations

import sqlite3
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.db import _db_io


class DbExecuteTool(BaseTool):
    name = "db_execute"
    description = "对沙箱内 SQLite 库执行单条写入/结构语句（INSERT/UPDATE/DDL）。一次仅一条；涉及 DROP/TRUNCATE 需 confirm_destructive=true；ATTACH/DETACH 与多语句被拒绝。返回受影响行数与（可选的）结果预览。"
    category = ToolCategory.DATA
    feature_group = "db"
    capability_tags = [ToolCapability.DATABASE.value]
    risk_level = "medium"
    is_read_only = False
    is_idempotent = False
    is_destructive = True
    aliases = ("execute_sql", "db_write")
    parameters = {
        "type": "object",
        "properties": {
            "db_path": {
                "type": "string",
                "description": "目标库文件的绝对路径（须位于数据沙箱内）。",
            },
            "statement": {"type": "string", "description": "单条 SQL 语句（DML/DDL）。"},
            "params": {
                "type": "object",
                "description": "命名参数绑定（如 :name），防止注入；数组会自动转 p0/p1...。",
            },
            "confirm_destructive": {
                "type": "boolean",
                "default": False,
                "description": "语句含 DROP/TRUNCATE/ALTER...DROP 时须显式确认。",
            },
            "timeout_seconds": {"type": "integer", "default": 30},
        },
        "required": ["db_path", "statement"],
    }

    def __init__(self, data_root: str | None = None) -> None:
        self.data_root = data_root or str(_db_io.DEFAULT_DATA_ROOT)

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        try:
            return self._execute_sync(params)
        except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error, OSError) as e:
            return _db_io.fail(str(e))

    def _execute_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        db_path = params["db_path"]
        statement = params.get("statement")
        if not statement or not isinstance(statement, str):
            raise ValueError("'statement' must be a non-empty string")
        single = _db_io.assert_single_statement(statement)
        _db_io.assert_no_attach(single)
        if _db_io.is_destructive(single) and not bool(params.get("confirm_destructive")):
            raise ValueError(
                "This SQL drops data (DROP/TRUNCATE/ALTER...DROP). "
                "Pass confirm_destructive: true when the user explicitly requested it."
            )
        bind = _db_io.coerce_params(params.get("params"))
        timeout = int(params.get("timeout_seconds") or 30)
        conn = _db_io.connect(db_path, self.data_root, timeout)
        try:
            cursor = conn.execute(single, bind) if bind else conn.execute(single)
            rowcount = cursor.rowcount if cursor.rowcount is not None else -1
            conn.commit()
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                preview = [dict(row) for row in cursor.fetchmany(10)]
                return _db_io.ok(
                    rowcount=rowcount, returns_rows=True, columns=columns, preview_rows=preview
                )
            return _db_io.ok(rowcount=rowcount, returns_rows=False)
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()
