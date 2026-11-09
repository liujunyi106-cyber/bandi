# 功能：pdf_reader schema 契约（merge 不强制 file_path / extract_metadata 声明 / read 运行时必填）
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.doc.pdf_reader import PDFReaderTool


def _make_pdf(path: Path, pages: int = 1) -> None:
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def test_pdf_schema_declares_extract_metadata_parameter() -> None:
    schema = PDFReaderTool().parameters
    assert "extract_metadata" in schema["properties"], "read 消费的 extract_metadata 必须声明"


def test_pdf_merge_does_not_require_file_path(tmp_path: Path) -> None:
    """merge 只用 merge_files；schema 不得以 required 强制 file_path（executor 会拒绝）。"""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_pdf(a, 1)
    _make_pdf(b, 1)
    tool = PDFReaderTool()
    params = {
        "operation": "merge",
        "merge_files": [str(a), str(b)],
        "output_path": str(tmp_path / "merged.pdf"),
    }
    assert tool.validate_params(params) == []


@pytest.mark.asyncio
async def test_pdf_merge_runs_without_file_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_pdf(a, 1)
    _make_pdf(b, 2)
    tool = PDFReaderTool()
    result = await tool.execute(
        {
            "operation": "merge",
            "merge_files": [str(a), str(b)],
            "output_path": str(tmp_path / "merged.pdf"),
        }
    )
    assert result["success"] is True
    assert result["total_pages"] == 3


@pytest.mark.asyncio
async def test_pdf_read_still_requires_file_path_at_runtime() -> None:
    result = await PDFReaderTool().execute({"operation": "read"})
    assert result["success"] is False
    assert "file_path" in result["error"]
