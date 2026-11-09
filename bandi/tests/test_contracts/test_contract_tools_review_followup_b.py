# 功能：契约——tools 域复查加固 B：folder add_file 源沙箱 / excel 空单元格列结构 /
from __future__ import annotations

from pathlib import Path

import pytest

# ---------- folder add_file source_path 沙箱 ----------


@pytest.mark.asyncio
async def test_contract_folder_add_file_source_sandboxed(tmp_path: Path, monkeypatch) -> None:
    """契约（沙箱·破坏者）：add_file(source_path=...) 必须限工作区——修复前
    Path(source_path) 直取任意路径并把文件移入（任意读 + 跨区移动）。"""
    from bandi.tools.util.folder_tool import FolderOperationsTool

    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")

    tool = FolderOperationsTool()
    result = await tool.execute(
        {
            "operation": "add_file",
            "folder": ".",
            "file": "x.txt",
            "source_path": str(outside),
        }
    )
    assert result.get("ok") is False, f"越界源路径必须拒绝: {result}"
    assert outside.exists(), "越界文件不得被移动/删除"


@pytest.mark.asyncio
async def test_contract_folder_add_file_source_inside_workspace_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """回归：工作区内 source_path 移动语义保持可用。"""
    from bandi.tools.util.folder_tool import FolderOperationsTool

    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")

    tool = FolderOperationsTool()
    result = await tool.execute(
        {"operation": "add_file", "folder": ".", "file": "dst.txt", "source_path": str(src)}
    )
    assert result.get("ok") is True, result
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "data"


# ---------- excel 空单元格列结构 ----------


@pytest.mark.asyncio
async def test_contract_excel_empty_cells_no_column_shift(tmp_path: Path, monkeypatch) -> None:
    """契约（列结构·破坏者）：include_empty_cells=False 不得使空单元格之后的列
    前移错位——修复前整行过滤 None，C 列值落到 B 列名下。"""
    from openpyxl import Workbook

    from bandi.tools.doc.excel_reader import ExcelReaderTool

    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    wb = Workbook()
    ws = wb.active
    ws.append(["a", "b", "c"])
    ws.append([1, None, 3])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    wb.close()

    result = await ExcelReaderTool().execute({"file_path": str(p), "include_empty_cells": False})
    row = result["data"][0]
    assert row.get("c") == 3, f"C 列不得错位: {row}"
    assert "b" not in row, f"空值单元格应省略键: {row}"

    result2 = await ExcelReaderTool().execute({"file_path": str(p)})
    assert result2["data"][0] == {"a": 1, "b": None, "c": 3}, "默认形态结构保持"


# ---------- CSV 引号内换行（RFC4180） ----------


@pytest.mark.asyncio
async def test_contract_csv_quoted_newline_preserved(tmp_path: Path, monkeypatch) -> None:
    """契约（RFC4180·破坏者）：引号内换行是合法字段内容，不得按行切断——
    修复前 splitlines() 先行截断导致行结构错乱。"""
    from bandi.tools.doc.csv_processor import CSVProcessorTool

    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    p = tmp_path / "t.csv"
    p.write_bytes(b'a,b\n"x\ny",2\n')

    result = await CSVProcessorTool().execute({"operation": "read", "file_path": str(p)})
    rows = result["rows"]
    assert len(rows) == 1, f"引号内换行不得拆成多行: {rows}"
    assert rows[0]["a"] == "x\ny", f"字段内容须保真: {rows[0]}"
    assert rows[0]["b"] == "2"
