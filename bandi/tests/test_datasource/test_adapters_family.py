"""契约族：datasource 适配器统一行为（csv/parquet/sqlite/xlsx）。

- 公共行为（inspect_schema/preview_table/run_sql_readonly）经统一数据面参数化；
- 各适配器独有测试（编码/空值/列选择/只读拒绝等）按段保留。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from bandi.datasource.adapters.csv import CsvAdapter
from bandi.datasource.adapters.parquet import ParquetAdapter
from bandi.datasource.adapters.sqlite import SqliteAdapter
from bandi.datasource.adapters.xlsx import XlsxAdapter
from bandi.datasource.core import AdapterExecutionInput, AdapterPreviewInput, AdapterSqlInput

# ==================== 公共行为参数化（统一数据面：employees 三行）====================


@pytest.fixture(params=["csv", "parquet", "sqlite", "xlsx"])
def adapter_case(request, tmp_path, monkeypatch):
    """功能：统一数据面 fixture——employees(id,name,department) 三行数据；
    四个适配器各自构造并返回 (adapter, 表名, 首行 id 期望值)。"""
    kind = request.param
    table = "employees"
    if kind == "csv":
        path = tmp_path / "employees.csv"
        path.write_text(
            "id,name,department\n1,Alice,Engineering\n2,Bob,Marketing\n3,Charlie,Engineering\n",
            encoding="utf-8",
        )
        from bandi.datasource.adapters.csv import CsvAdapter

        return CsvAdapter({"path": str(tmp_path)}), table, "1"
    if kind == "parquet":
        pa = pytest.importorskip("pyarrow")
        import pyarrow.parquet as pq

        t = pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "name": pa.array(["Alice", "Bob", "Charlie"], pa.string()),
                "department": pa.array(["Engineering", "Marketing", "Engineering"], pa.string()),
            }
        )
        pq.write_table(t, str(tmp_path / "employees.parquet"))
        from bandi.datasource.adapters.parquet import ParquetAdapter

        return ParquetAdapter({"path": str(tmp_path)}), table, 1
    if kind == "sqlite":
        import sqlite3

        db = tmp_path / "emp.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE employees (id INTEGER, name TEXT, department TEXT)")
        conn.executemany(
            "INSERT INTO employees VALUES (?, ?, ?)",
            [
                (1, "Alice", "Engineering"),
                (2, "Bob", "Marketing"),
                (3, "Charlie", "Engineering"),
            ],
        )
        conn.commit()
        conn.close()
        from bandi.datasource.adapters.sqlite import SqliteAdapter

        return SqliteAdapter({"path": str(db)}), table, 1
    # xlsx：mock openpyxl（生产依赖可选——monkeypatch 自动恢复防泄漏）
    from unittest.mock import MagicMock

    mock_openpyxl = MagicMock()
    wb = MagicMock()
    wb.sheetnames = ["employees"]
    ws = MagicMock()
    wb.__getitem__.return_value = ws
    ws.iter_rows.return_value = iter(
        [
            ("id", "name", "department"),
            (1, "Alice", "Engineering"),
            (2, "Bob", "Marketing"),
            (3, "Charlie", "Engineering"),
        ]
    )
    mock_openpyxl.load_workbook.return_value = wb
    monkeypatch.setitem(sys.modules, "openpyxl", mock_openpyxl)
    from bandi.datasource.adapters.xlsx import XlsxAdapter

    return XlsxAdapter({"path": "/fake/test.xlsx"}), table, 1


class TestAdapterCommonBehavior:
    @pytest.mark.asyncio
    async def test_inspect_schema(self, adapter_case) -> None:
        adapter, table, _ = adapter_case
        schema = await adapter.inspect_schema(AdapterExecutionInput())
        assert len(schema.tables) == 1
        assert schema.tables[0].name == table
        col_names = [c.name for c in schema.tables[0].columns]
        assert col_names[:3] == ["id", "name", "department"]

    @pytest.mark.asyncio
    async def test_preview_table(self, adapter_case) -> None:
        adapter, table, _ = adapter_case
        result = await adapter.preview_table(AdapterPreviewInput(table=table, limit=10))
        assert result.row_count == 3
        assert result.columns == ["id", "name", "department"]
        assert result.rows[0][1] == "Alice"
        assert result.rows[0][2] == "Engineering"

    @pytest.mark.asyncio
    async def test_run_sql_readonly(self, adapter_case) -> None:
        adapter, table, _ = adapter_case
        result = await adapter.run_sql_readonly(
            AdapterSqlInput(sql=f"SELECT name, department FROM {table} LIMIT 2")
        )
        assert result.row_count == 2
        assert result.columns == ["name", "department"]
        assert result.rows[0][0] == "Alice"


# ==================== test_adapters_csv ====================


@pytest.fixture
def csv_dir() -> str:
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "employees.csv"), "w", newline="", encoding="utf-8") as f:
        f.write("id,name,department\n")
        f.write("1,Alice,Engineering\n")
        f.write("2,Bob,Marketing\n")
        f.write("3,Charlie,Engineering\n")
    yield d
    for f in os.listdir(d):
        os.unlink(os.path.join(d, f))
    os.rmdir(d)


class TestCsvAdapter:
    @pytest.mark.asyncio
    async def test_adapter_with_single_file(self, csv_dir: str) -> None:
        csv_path = os.path.join(csv_dir, "employees.csv")
        adapter = CsvAdapter({"path": csv_path})
        schema = await adapter.inspect_schema(AdapterExecutionInput())
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "employees"

    @pytest.mark.asyncio
    async def test_run_sql_unknown_column_raises(self, csv_dir: str) -> None:
        """
        返回部分结果（SELECT id, ghost 只返回 id 且无任何提示）。"""
        adapter = CsvAdapter({"path": csv_dir})
        with pytest.raises(ValueError, match="unknown column"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT id, ghost FROM employees"))

    @pytest.mark.asyncio
    async def test_run_sql_reads_only_limit_rows(self, csv_dir: str, monkeypatch) -> None:
        """
        GB 级文件 OOM）。"""
        import bandi.datasource.adapters.csv as csv_mod

        adapter = CsvAdapter({"path": csv_dir})
        seen: list[int] = []

        def _tracking_read(fp, limit=0):
            seen.append(limit)
            return csv_mod.CsvAdapter._read_csv(adapter, fp, limit)

        monkeypatch.setattr(adapter, "_read_csv", _tracking_read)
        result = await adapter.run_sql_readonly(
            AdapterSqlInput(sql="SELECT * FROM employees LIMIT 2")
        )
        assert result.row_count == 2
        assert seen == [2], "limit 必须透传到读取层（防全量读内存）"

    @pytest.mark.asyncio
    async def test_empty_column_infers_text(self) -> None:
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "empty_col.csv"), "w", newline="", encoding="utf-8") as f:
                f.write("id,note\n")
                f.write("1,\n")
                f.write("2,\n")
            adapter = CsvAdapter({"path": d})
            schema = await adapter.inspect_schema(AdapterExecutionInput())
            note_col = [c for c in schema.tables[0].columns if c.name == "note"][0]
            assert note_col.type == "TEXT", "空列必须判 TEXT 而非 INTEGER"
        finally:
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)

    @pytest.mark.asyncio
    async def test_gbk_encoding_config(self) -> None:
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "cn.csv"), "w", newline="", encoding="gbk") as f:
                f.write("id,name\n")
                f.write("1,张三\n")
                f.write("2,李四\n")
            adapter = CsvAdapter({"path": d, "encoding": "gbk"})
            result = await adapter.preview_table(AdapterPreviewInput(table="cn", limit=10))
            assert result.rows[0] == ["1", "张三"]
        finally:
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)


# ==================== test_adapters_parquet ====================


pytest.importorskip("pyarrow")


@pytest.fixture
def parquet_dir() -> str:
    d = tempfile.mkdtemp()
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "name": pa.array(["Alice", "Bob", "Charlie"], pa.string()),
            "score": pa.array([95.5, 87.0, 91.2], pa.float64()),
        }
    )
    pq.write_table(table, os.path.join(d, "students.parquet"))
    yield d
    for f in os.listdir(d):
        os.unlink(os.path.join(d, f))
    os.rmdir(d)


class TestParquetAdapter:
    @pytest.mark.asyncio
    async def test_adapter_with_single_file(self, parquet_dir: str) -> None:
        pq_path = os.path.join(parquet_dir, "students.parquet")
        adapter = ParquetAdapter({"path": pq_path})
        schema = await adapter.inspect_schema(AdapterExecutionInput())
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "students"

    @pytest.mark.asyncio
    async def test_missing_table_returns_empty(self, parquet_dir: str) -> None:
        adapter = ParquetAdapter({"path": parquet_dir})
        result = await adapter.preview_table(AdapterPreviewInput(table="nonexistent", limit=10))
        assert result.row_count == 0

    @pytest.mark.asyncio
    async def test_run_sql_unknown_column_raises(self, parquet_dir: str) -> None:
        adapter = ParquetAdapter({"path": parquet_dir})
        with pytest.raises(ValueError, match="unknown column"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT id, ghost FROM students"))

    @pytest.mark.asyncio
    async def test_limit_zero_returns_empty(self, parquet_dir: str) -> None:
        adapter = ParquetAdapter({"path": parquet_dir})
        result = await adapter.run_sql_readonly(
            AdapterSqlInput(sql="SELECT * FROM students LIMIT 0")
        )
        assert result.row_count == 0

    @pytest.mark.asyncio
    async def test_negative_limit_returns_empty(self, parquet_dir: str) -> None:
        adapter = ParquetAdapter({"path": parquet_dir})
        result = await adapter.preview_table(AdapterPreviewInput(table="students", limit=-1))
        assert result.row_count == 0

    @pytest.mark.asyncio
    async def test_null_values_stay_none(self, parquet_dir: str) -> None:
        import os as _os

        import pyarrow as pa
        import pyarrow.parquet as pq

        d = tempfile.mkdtemp()
        try:
            t = pa.table(
                {"id": pa.array([1, 2], pa.int64()), "note": pa.array(["a", None], pa.string())}
            )
            pq.write_table(t, _os.path.join(d, "nulls.parquet"))
            adapter = ParquetAdapter({"path": d})
            result = await adapter.preview_table(AdapterPreviewInput(table="nulls", limit=10))
            assert result.rows[1][1] is None, f"null 必须保持 None，实际 {result.rows[1][1]!r}"
            assert result.rows[0][1] == "a"
        finally:
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)


# ==================== test_adapters_sqlite ====================


@pytest.fixture
def db_path() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
    conn.execute("INSERT INTO users VALUES (2, 'Bob', 25)")
    conn.execute("INSERT INTO users VALUES (3, 'Charlie', 35)")
    conn.commit()
    conn.close()
    yield f.name
    os.unlink(f.name)


@pytest.fixture
def adapter(db_path: str) -> SqliteAdapter:
    return SqliteAdapter({"path": db_path})


class TestSqliteAdapter:
    @pytest.mark.asyncio
    async def test_preview_table_with_limit(self, adapter: SqliteAdapter) -> None:
        result = await adapter.preview_table(AdapterPreviewInput(table="users", limit=2))
        assert result.row_count == 2

    @pytest.mark.asyncio
    async def test_run_sql_apply_limit(self, adapter: SqliteAdapter) -> None:
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM users", limit=1))
        assert result.row_count == 1

    @pytest.mark.asyncio
    async def test_empty_table(self) -> None:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = sqlite3.connect(f.name)
        conn.execute("CREATE TABLE empty (id INTEGER)")
        conn.commit()
        conn.close()
        try:
            adapter = SqliteAdapter({"path": f.name})
            result = await adapter.preview_table(AdapterPreviewInput(table="empty", limit=10))
            assert result.row_count == 0
            assert result.columns == ["id"]
        finally:
            os.unlink(f.name)

    @pytest.mark.asyncio
    async def test_preview_table_unknown_table_rejected(self, adapter: SqliteAdapter) -> None:
        with pytest.raises(ValueError, match="unknown table"):
            await adapter.preview_table(
                AdapterPreviewInput(table="users; DROP TABLE users", limit=10)
            )

    @pytest.mark.asyncio
    async def test_preview_table_case_whitelist(self, adapter: SqliteAdapter) -> None:
        with pytest.raises(ValueError, match="unknown table"):
            await adapter.preview_table(AdapterPreviewInput(table="'users' -- ", limit=10))

    @pytest.mark.asyncio
    async def test_run_sql_rejects_write_statements(self, adapter: SqliteAdapter) -> None:
        with pytest.raises(ValueError, match="read-only"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="DROP TABLE users"))
        with pytest.raises(ValueError, match="read-only"):
            await adapter.run_sql_readonly(
                AdapterSqlInput(sql="INSERT INTO users VALUES (9, 'x', 1)")
            )
        with pytest.raises(ValueError, match="read-only"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="UPDATE users SET name='x'"))
        with pytest.raises(ValueError, match="read-only"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="DELETE FROM users"))

    @pytest.mark.asyncio
    async def test_run_sql_accepts_select_variants(self, adapter: SqliteAdapter) -> None:
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM users"))
        assert result.row_count == 3
        # OperationalError，现提前 ValueError 明确拒绝）
        with pytest.raises(ValueError, match="only read-only"):
            await adapter.run_sql_readonly(
                AdapterSqlInput(sql="SELECT * FROM users; DROP TABLE users")
            )

    @pytest.mark.asyncio
    async def test_preview_table_rejects_quoted_table_name(self) -> None:
        """
        引号表名（`we"ird` → 坏 SQL），白名单外显式拒绝。"""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = sqlite3.connect(f.name)
        conn.execute('CREATE TABLE "we""ird" (x INTEGER)')
        conn.commit()
        conn.close()
        try:
            adapter = SqliteAdapter({"path": f.name})
            with pytest.raises(ValueError, match="unknown table"):
                await adapter.preview_table(AdapterPreviewInput(table='we"ird', limit=10))
        finally:
            os.unlink(f.name)


# ==================== test_adapters_xlsx ====================


@pytest.fixture
def mock_xlsx():
    mock_openpyxl = MagicMock()
    wb = MagicMock()
    mock_openpyxl.load_workbook.return_value = wb
    with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
        yield wb


class TestXlsxAdapter:
    @pytest.mark.asyncio
    async def test_run_sql_readonly_invalid(self) -> None:
        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        with pytest.raises(ValueError, match="only supports simple SELECT"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="DROP TABLE Sheet1"))

    @pytest.mark.asyncio
    async def test_run_sql_readonly_table_not_found(self, mock_xlsx: MagicMock) -> None:
        mock_xlsx.sheetnames = ["Sheet1"]

        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM MissingSheet"))
        assert result.row_count == 0

    @pytest.mark.asyncio
    async def test_run_sql_readonly_column_selection(self, mock_xlsx: MagicMock) -> None:
        """
        恒返回全部列（SELECT name FROM Sheet1 返回 id+name 两列）。"""
        mock_xlsx.sheetnames = ["Sheet1"]
        ws = MagicMock()
        mock_xlsx.__getitem__.return_value = ws
        ws.iter_rows.return_value = iter(
            [
                ("id", "name"),
                (1, "Alice"),
                (2, "Bob"),
                (3, "Charlie"),
            ]
        )

        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT name FROM Sheet1"))
        assert result.columns == ["name"]
        assert result.rows == [["Alice"], ["Bob"], ["Charlie"]]

    @pytest.mark.asyncio
    async def test_run_sql_readonly_unknown_column_raises(self, mock_xlsx: MagicMock) -> None:
        mock_xlsx.sheetnames = ["Sheet1"]
        ws = MagicMock()
        mock_xlsx.__getitem__.return_value = ws
        ws.iter_rows.return_value = iter([("id", "name"), (1, "Alice")])

        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        with pytest.raises(ValueError, match="unknown column"):
            await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT id, ghost FROM Sheet1"))

    @pytest.mark.asyncio
    async def test_preview_missing_sheet_returns_empty(self, mock_xlsx: MagicMock) -> None:
        """
        KeyError，必须返回空 TableResult（与 csv/parquet 契约一致）。"""
        mock_xlsx.sheetnames = ["Sheet1"]

        def _raise_keyerror(name):
            raise KeyError(name)

        mock_xlsx.__getitem__.side_effect = _raise_keyerror
        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        result = await adapter.preview_table(AdapterPreviewInput(table="Missing", limit=10))
        assert result.row_count == 0
        assert result.columns == []

    @pytest.mark.asyncio
    async def test_sheet_names_empty(self, mock_xlsx: MagicMock) -> None:
        mock_xlsx.sheetnames = []

        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        schema = await adapter.inspect_schema(AdapterExecutionInput())
        assert len(schema.tables) == 0

    @pytest.mark.asyncio
    async def test_run_sql_readonly_passes_limit_to_read(self, mock_xlsx: MagicMock) -> None:
        """深度复审修复：xlsx run_sql 必须透传 limit 到读取层（与 csv/parquet"""
        mock_xlsx.sheetnames = ["Sheet1"]
        ws = MagicMock()
        mock_xlsx.__getitem__.return_value = ws
        ws.iter_rows.return_value = iter(
            [
                ("id", "name"),
                (1, "Alice"),
                (2, "Bob"),
                (3, "Charlie"),
            ]
        )

        adapter = XlsxAdapter({"path": "/fake/test.xlsx"})
        with patch.object(adapter, "_read_sheet", wraps=adapter._read_sheet) as spy:
            result = await adapter.run_sql_readonly(
                AdapterSqlInput(sql="SELECT * FROM Sheet1", limit=2)
            )
            assert spy.call_args.kwargs.get("limit") == 2, (
                "run_sql 必须把 limit 透传给 _read_sheet（读取层截断防 OOM）"
            )
        assert result.row_count == 2
