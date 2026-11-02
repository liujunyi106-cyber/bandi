# 功能：DocumentReadTool 测试 — markdown/csv/pdf + 沙箱/敏感路径

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from bandi.tools.doc_tool import DocumentReadTool


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocumentReadTool:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return DocumentReadTool()


class TestMarkdownRead:
    @pytest.mark.asyncio
    async def test_read_markdown(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / "notes.md"
        f.write_text("# Title\n\nSome body text.", encoding="utf-8")
        result = await tool.execute({"path": "notes.md"})
        assert result["success"] is True
        assert result["format"] == "text"
        assert "# Title" in result["content"]
        assert result["line_count"] == 3

    @pytest.mark.asyncio
    async def test_explicit_format_markdown(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / "data.unknown"
        f.write_text("just text", encoding="utf-8")
        result = await tool.execute({"path": "data.unknown", "format": "markdown"})
        assert result["success"] is True
        assert result["content"] == "just text"

    @pytest.mark.asyncio
    async def test_missing_file(self, tool: DocumentReadTool) -> None:
        result = await tool.execute({"path": "missing.md"})
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_path_param(self, tool: DocumentReadTool) -> None:
        result = await tool.execute({})
        assert result["success"] is False
        assert "path is required" in result["error"]


class TestCsvRead:
    @pytest.mark.asyncio
    async def test_read_csv(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
        result = await tool.execute({"path": "data.csv"})
        assert result["success"] is True
        assert result["format"] == "csv"
        assert result["columns"] == ["name", "age"]
        assert result["rows"] == [["Alice", "30"], ["Bob", "25"]]
        assert result["row_count"] == 2

    @pytest.mark.asyncio
    async def test_empty_csv(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / "empty.csv"
        f.write_text("", encoding="utf-8")
        result = await tool.execute({"path": "empty.csv"})
        assert result["success"] is True
        assert result["columns"] == []
        assert result["row_count"] == 0


class TestPdfRead:
    @pytest.mark.asyncio
    async def test_pdf_missing_pypdf(
        self, tmp_path: Path, tool: DocumentReadTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pypdf", None)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        result = await tool.execute({"path": "doc.pdf"})
        assert result["success"] is False
        assert "pypdf" in result["error"]

    @pytest.mark.asyncio
    async def test_pdf_extract_text(
        self, tmp_path: Path, tool: DocumentReadTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakePage:
            def extract_text(self) -> str:
                return "Hello from page one."

        class _FakeReader:
            def __init__(self, *args, **kwargs):
                pass

            @property
            def pages(self):
                return [_FakePage()]

        fake = ModuleType("pypdf")
        fake.PdfReader = _FakeReader
        monkeypatch.setitem(sys.modules, "pypdf", fake)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        result = await tool.execute({"path": "doc.pdf"})
        assert result["success"] is True
        assert result["format"] == "pdf"
        assert "Hello from page one." in result["content"]
        assert result["page_count"] == 1


class TestPdfReadReal:
    def _make_real_pdf(self, path: Path) -> None:
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello from real pypdf")
        doc.save(str(path))
        doc.close()

    @pytest.mark.asyncio
    async def test_real_pdf_extract_text(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / "doc.pdf"
        self._make_real_pdf(f)
        result = await tool.execute({"path": "doc.pdf"})
        assert result["success"] is True, result
        assert result["format"] == "pdf"
        assert "Hello from real pypdf" in result["content"]
        assert result["page_count"] == 1
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_real_pdf_multi_page(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        import fitz

        doc = fitz.open()
        for text in ("Page one", "Page two"):
            page = doc.new_page()
            page.insert_text((72, 72), text)
        f = tmp_path / "two.pdf"
        doc.save(str(f))
        doc.close()
        result = await tool.execute({"path": "two.pdf"})
        assert result["success"] is True
        assert result["page_count"] == 2
        assert "Page one" in result["content"]
        assert "Page two" in result["content"]


class TestSafety:
    @pytest.mark.asyncio
    async def test_path_outside_workspace(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        outside = tmp_path.parent / "escape.md"
        outside.write_text("secret", encoding="utf-8")
        result = await tool.execute({"path": str(outside)})
        assert result["success"] is False
        assert "outside workspace" in result["error"]

    @pytest.mark.asyncio
    async def test_sensitive_path_rejected(self, tmp_path: Path, tool: DocumentReadTool) -> None:
        f = tmp_path / ".env"
        f.write_text("KEY=value", encoding="utf-8")
        result = await tool.execute({"path": ".env"})
        assert result["success"] is False
        assert "sensitive path" in result["error"]

    @pytest.mark.asyncio
    async def test_traversal_rejected(self, tool: DocumentReadTool) -> None:
        result = await tool.execute({"path": "../secret.md"})
        assert result["success"] is False
        assert "outside workspace" in result["error"]
