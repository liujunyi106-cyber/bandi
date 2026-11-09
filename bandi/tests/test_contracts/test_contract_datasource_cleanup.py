# 功能：datasource 边角清理契约（类型推断严格化/表名正则/LIMIT 逗号拒绝/busy_timeout/单次打开/close）
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.datasource.adapters.csv import CsvAdapter
from bandi.datasource.adapters.parquet import ParquetAdapter
from bandi.datasource.adapters.sqlite import SqliteAdapter
from bandi.datasource.adapters.xlsx import XlsxAdapter
from bandi.datasource.core import AdapterExecutionInput, AdapterPreviewInput, AdapterSqlInput

# ── csv 类型推断严格化（Python 字面量特例不误判）─────────────────────────


@pytest.mark.asyncio
async def test_csv_type_inference_rejects_python_literal_oddities(tmp_path: Path) -> None:
    """`1_0`（下划线数字）/`nan`/`inf` 不得推断为 INTEGER/；科学计数法保留 。"""
    path = tmp_path / "odd.csv"
    path.write_text(
        "under,nan_col,inf_col,sci\n1_0,nan,inf,1e5\n2_0,nan,inf,2e5\n",
        encoding="utf-8",
    )
    adapter = CsvAdapter({"path": str(tmp_path)})
    schema = await adapter.inspect_schema(AdapterExecutionInput())
    types = {c.name: c.type for c in schema.tables[0].columns}
    assert types["under"] == "TEXT"
    assert types["nan_col"] == "TEXT"
    assert types["inf_col"] == "TEXT"
    assert types["sci"] == "FLOAT"


# ── 表名正则：带连字符/中文的表名（原 `\w+` 拒绝连字符）───────────────────


@pytest.mark.asyncio
async def test_csv_sql_accepts_hyphenated_table_name(tmp_path: Path) -> None:
    (tmp_path / "my-data.csv").write_text("id,name\n1,Alice\n", encoding="utf-8")
    adapter = CsvAdapter({"path": str(tmp_path)})
    result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM my-data"))
    assert result.row_count == 1
    assert result.rows[0] == ["1", "Alice"]


@pytest.mark.asyncio
async def test_xlsx_sql_accepts_hyphenated_sheet_name(tmp_path: Path) -> None:
    import openpyxl

    path = tmp_path / "wb.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "my-sheet"
    ws.append(["id", "name"])
    ws.append([1, "Alice"])
    wb.save(path)
    adapter = XlsxAdapter({"path": str(path)})
    result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM my-sheet"))
    assert result.row_count == 1
    assert result.rows[0] == [1, "Alice"]


@pytest.mark.asyncio
async def test_parquet_sql_accepts_hyphenated_table_name(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    pq.write_table(pa.table({"id": [1]}), tmp_path / "my-data.parquet")
    adapter = ParquetAdapter({"path": str(tmp_path)})
    result = await adapter.run_sql_readonly(AdapterSqlInput(sql="SELECT * FROM my-data"))
    assert result.row_count == 1
    assert result.rows[0] == [1]


# ── sqlite 显式 busy_timeout ────────────────────────────────────────────


def test_sqlite_connect_sets_explicit_busy_timeout() -> None:
    adapter = SqliteAdapter({"path": ":memory:"})
    conn = adapter._connect()
    try:
        value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert value == 5000, f"busy_timeout 必须显式 5000ms（实读 {value}）"
    finally:
        conn.close()


# ── xlsx 单次打开 + 类型推断 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_xlsx_preview_opens_workbook_once(tmp_path: Path, monkeypatch) -> None:
    """preview_table 不得双开工作簿（原 `_sheet_names`+`_read_sheet` 各开一次）。"""
    import openpyxl

    path = tmp_path / "one.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["id", "name"])
    ws.append([1, "Alice"])
    wb.save(path)

    calls: list[int] = []
    original_open = XlsxAdapter._open

    def counting_open(self):
        calls.append(1)
        return original_open(self)

    monkeypatch.setattr(XlsxAdapter, "_open", counting_open)
    adapter = XlsxAdapter({"path": str(path)})
    result = await adapter.preview_table(AdapterPreviewInput(table="S1", limit=10))
    assert result.row_count == 1
    assert len(calls) == 1, f"preview 只应打开工作簿一次（实际 {len(calls)} 次）"


@pytest.mark.asyncio
async def test_xlsx_inspect_schema_infers_column_types(tmp_path: Path) -> None:
    """id（int）→ INTEGER / name（str）→ TEXT，不得全列硬编码 TEXT。"""
    import openpyxl

    path = tmp_path / "typed.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["id", "name", "score"])
    ws.append([1, "Alice", 91.5])
    ws.append([2, "Bob", 88.0])
    wb.save(path)
    adapter = XlsxAdapter({"path": str(path)})
    schema = await adapter.inspect_schema(AdapterExecutionInput())
    types = {c.name: c.type for c in schema.tables[0].columns}
    assert types["id"] == "INTEGER"
    assert types["name"] == "TEXT"
    assert types["score"] == "FLOAT"


# ── parquet 句柄关闭 ────────────────────────────────────────────────────


def test_parquet_metadata_closes_file(monkeypatch, tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"id": [1, 2]}), path)
    adapter = ParquetAdapter({"path": str(path)})

    closes: list[int] = []
    original_pf = pq.ParquetFile

    class _SpyParquetFile:
        def __init__(self, fp):
            self._inner = original_pf(fp)

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def close(self) -> None:
            closes.append(1)
            self._inner.close()

    monkeypatch.setattr(pq, "ParquetFile", _SpyParquetFile)
    columns, _types, _rows = adapter._read_parquet_metadata(path)
    assert columns == ["id"]
    assert closes, "ParquetFile 读取后必须显式关闭"
