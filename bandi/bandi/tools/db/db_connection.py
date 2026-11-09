# 功能：db_connect——沙箱内新建 sqlite 库 / 连通性自检（create_sqlite·overwrite 门控·test_connection）
from __future__ import annotations

import sqlite3
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.db import _db_io
from bandi.utils.db import connect


class DbConnectTool(BaseTool):
    name = "db_connect"
    description = "新建 SQLite 数据库或验证现有数据库连接。create_sqlite 仅在 .bandi/data 沙箱内落库；同路径已存在需 overwrite=true 才覆盖。"
    category = ToolCategory.DATA
    feature_group = "db"
    capability_tags = [ToolCapability.DATABASE.value]
    risk_level = "medium"
    is_read_only = False
    is_idempotent = False
    aliases = ("connect_db", "db_create")
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create_sqlite", "test_connection"],
                "default": "test_connection",
                "description": "create_sqlite=新建库；test_connection=验证库可连。",
            },
            "db_path": {
                "type": "string",
                "description": "目标库文件的绝对路径（必须位于数据沙箱目录内）。",
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "create_sqlite 时同路径已存在是否允许覆盖。",
            },
            "timeout_seconds": {"type": "integer", "default": 30},
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
            op = params.get("operation") or "test_connection"
            if op == "create_sqlite":
                return self._create_sqlite(params["db_path"], bool(params.get("overwrite")))
            if op == "test_connection":
                return self._test_connection(
                    params["db_path"], int(params.get("timeout_seconds") or 30)
                )
            raise ValueError(f"unknown operation: {op}")
        except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error, OSError) as e:
            return _db_io.fail(str(e))

    def _create_sqlite(self, db_path: str, overwrite: bool) -> dict[str, Any]:
        target = _db_io.resolve_db_path(db_path, self.data_root)
        if target.exists():
            if not overwrite:
                raise ValueError(
                    f"database file already exists: {target}; pass overwrite: true to replace"
                )
            if not target.is_file():
                raise ValueError("db_path is not a regular file")
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(target, timeout=30.0)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return _db_io.ok(sqlite_path=str(target))

    def _test_connection(self, db_path: str, timeout_seconds: int) -> dict[str, Any]:
        conn = _db_io.connect(db_path, self.data_root, timeout_seconds)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return _db_io.ok(kind="sqlite")
