# 功能：db 工具族契约族——建库/写操作 safy 门控/路径沙箱/绑定参数/中文值/内省
# 行为族级覆盖（DoD-10）：db_connect create+test / db_execute 写+多语句+破坏性+ATTACH / db_inspect 内省
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bandi.tools.base import ToolPermissionContext


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute")


def _tools(data_root: str) -> Any:
    from bandi.tools.db.db_connection import DbConnectTool
    from bandi.tools.db.db_execute import DbExecuteTool
    from bandi.tools.db.db_inspect import DbInspectTool

    return (
        DbConnectTool(data_root=data_root),
        DbExecuteTool(data_root=data_root),
        DbInspectTool(data_root=data_root),
    )


def _seed_db(dir_path: Path) -> str:
    # 功能：在沙箱内建一个含表的最小 sqlite 库，返回 db 文件路径
    db_path = dir_path / "seed.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute('CREATE TABLE "订单" ("订单号" TEXT, "金额" INTEGER, PRIMARY KEY ("订单号"))')
    conn.commit()
    conn.close()
    return str(db_path)


# ---------- db_connect 族 ----------


@pytest.mark.asyncio
async def test_connect_create_sqlite_in_sandbox_succeeds(tmp_path: Path) -> None:
    connect, *_ = _tools(str(tmp_path))
    target = tmp_path / "新库.db"
    result = await connect.execute({"operation": "create_sqlite", "db_path": str(target)}, _ctx())
    assert result["success"] is True
    assert Path(result["sqlite_path"]).exists()
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_connect_create_existing_without_overwrite_fails(tmp_path: Path) -> None:
    connect, *_ = _tools(str(tmp_path))
    target = tmp_path / "dup.db"
    target.write_bytes(b"existing")
    result = await connect.execute({"operation": "create_sqlite", "db_path": str(target)}, _ctx())
    assert result["success"] is False
    assert "exists" in str(result.get("error", "")).lower()
    assert target.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_connect_create_overwrite_replaces_old_db(tmp_path: Path) -> None:
    connect, *_ = _tools(str(tmp_path))
    target = tmp_path / "re.db"
    conn = sqlite3.connect(str(target))
    conn.execute("CREATE TABLE old_t (id INTEGER)")
    conn.commit()
    conn.close()
    result = await connect.execute(
        {"operation": "create_sqlite", "db_path": str(target), "overwrite": True}, _ctx()
    )
    assert result["success"] is True
    conn = sqlite3.connect(str(target))
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "old_t" not in names


@pytest.mark.asyncio
async def test_connect_path_escaping_sandbox_fails(tmp_path: Path) -> None:
    connect, *_ = _tools(str(tmp_path))
    outside = tmp_path.parent / "evil.db"
    result = await connect.execute({"operation": "create_sqlite", "db_path": str(outside)}, _ctx())
    assert result["success"] is False
    assert (
        "sandbox" in str(result.get("error", "")).lower()
        or "data" in str(result.get("error", "")).lower()
    )
    assert not outside.exists()


@pytest.mark.asyncio
async def test_connect_test_connection_valid_db(tmp_path: Path) -> None:
    connect, *_ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await connect.execute({"operation": "test_connection", "db_path": db_path}, _ctx())
    assert result["success"] is True
    assert result["kind"] == "sqlite"


# ---------- db_execute 族 ----------


@pytest.mark.asyncio
async def test_execute_insert_chinese_value_is_readable_back(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await execute.execute(
        {
            "db_path": db_path,
            "statement": 'INSERT INTO "订单" ("订单号", "金额") VALUES (:no, :amt)',
            "params": {"no": "SO-中文", "amt": 88},
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["rowcount"] == 1
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute('SELECT "订单号", "金额" FROM "订单"').fetchone()
    finally:
        conn.close()
    assert row == ("SO-中文", 88)


@pytest.mark.asyncio
async def test_execute_multiple_statements_rejected(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await execute.execute(
        {"db_path": db_path, "statement": 'SELECT 1; DROP TABLE "订单"'}, _ctx()
    )
    assert result["success"] is False
    assert (
        "one statement" in str(result.get("error", "")).lower()
        or "statement" in str(result.get("error", "")).lower()
    )
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "订单" in names


@pytest.mark.asyncio
async def test_execute_destructive_without_confirm_fails(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await execute.execute({"db_path": db_path, "statement": 'DROP TABLE "订单"'}, _ctx())
    assert result["success"] is False
    assert "confirm_destructive" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_execute_destructive_with_confirm_succeeds(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await execute.execute(
        {
            "db_path": db_path,
            "statement": 'DROP TABLE "订单"',
            "confirm_destructive": True,
        },
        _ctx(),
    )
    assert result["success"] is True
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "订单" not in names


@pytest.mark.asyncio
async def test_execute_param_binding_is_not_injectable(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await execute.execute(
        {
            "db_path": db_path,
            "statement": 'INSERT INTO "订单" ("订单号", "金额") VALUES (:no, :amt)',
            "params": {"no": 'x\'; DROP TABLE "订单"; --', "amt": 1},
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["rowcount"] == 1
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute('SELECT "订单号" FROM "订单"').fetchone()
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert row is not None
    assert "订单" in names


@pytest.mark.asyncio
async def test_execute_attach_database_rejected(tmp_path: Path) -> None:
    _, execute, _ = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    outside = tmp_path / "attacked.db"
    outside.write_bytes(b"not-a-db")
    result = await execute.execute(
        {
            "db_path": db_path,
            "statement": f"ATTACH '{outside.as_posix()}' AS ghost",
        },
        _ctx(),
    )
    assert result["success"] is False


# ---------- db_inspect 族 ----------


@pytest.mark.asyncio
async def test_inspect_list_tables(tmp_path: Path) -> None:
    _, _, inspect = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await inspect.execute({"operation": "list_tables", "db_path": db_path}, _ctx())
    assert result["success"] is True
    assert "订单" in result["tables"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_inspect_describe_table_columns_pk(tmp_path: Path) -> None:
    _, _, inspect = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await inspect.execute(
        {"operation": "describe_table", "db_path": db_path, "table_name": "订单"}, _ctx()
    )
    assert result["success"] is True
    names = [c["name"] for c in result["columns"]]
    assert names == ["订单号", "金额"]
    assert "订单号" in result["primary_key"]


@pytest.mark.asyncio
async def test_inspect_describe_missing_table_fails(tmp_path: Path) -> None:
    _, _, inspect = _tools(str(tmp_path))
    db_path = _seed_db(tmp_path)
    result = await inspect.execute(
        {"operation": "describe_table", "db_path": db_path, "table_name": "不存在"}, _ctx()
    )
    assert result["success"] is False
    assert "不存在" in str(result.get("error", ""))
