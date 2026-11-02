# 功能：PDF 工具族测试 — PDFReaderTool 12 ops + pdf_research 4 工具（真实 fitz 生成 PDF + Mock LLM）

from __future__ import annotations

import pytest

from bandi.tools.doc.pdf_reader import PDFReaderTool
from bandi.tools.doc.pdf_research import (
    _SECTION_SUMMARY_MAX_TOKENS,
    _TRANSLATE_MAX_TOKENS,
    CitationExtractorTool,
    PDFStructureTool,
    PDFTranslateTool,
    SectionSummarizerTool,
)


def _make_pdf(path, pages: int = 2, with_outline: bool = False) -> None:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Title page {i + 1}")
        page.insert_text(
            (72, 100), "This is test content with a DOI 10.1234/example and url https://example.com"
        )
        page.insert_text((72, 140), "Figure 1: sample chart")
        page.insert_text((72, 170), "Abstract")
        page.insert_text((72, 200), "Introduction and method")
        page.insert_text((72, 240), "References")
        page.insert_text((72, 280), "[1] First author. Example paper. 2020.")
        page.insert_text((72, 320), "[2] Second author. Another work. 2021.")
    if with_outline:
        doc.set_toc([(1, "Chapter One", 1), (2, "Section 1.1", 1)])
    doc.save(str(path))
    doc.close()


@pytest.fixture
def pdf(tmp_path) -> str:
    path = tmp_path / "sample.pdf"
    _make_pdf(path)
    return str(path)


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> PDFReaderTool:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return PDFReaderTool()


class TestPDFReaderRead:
    @pytest.mark.asyncio
    async def test_read_full(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf})
        assert result["success"] is True
        assert result["total_pages"] == 2
        assert "Title page 1" in result["text"]
        assert result["extracted_pages"] == 2

    @pytest.mark.asyncio
    async def test_read_page_range(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "start_page": 2, "end_page": 2})
        assert result["success"] is True
        assert result["extracted_pages"] == 1
        assert "page 2" in result["text"]
        assert "page 1" not in result["text"]

    @pytest.mark.asyncio
    async def test_read_max_chars(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "max_chars": 100})
        assert result["success"] is True
        assert result["truncated"] is True
        assert len(result["text"]) <= 100

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": "nope.pdf"})
        assert result["success"] is False
        assert "not a PDF" in result["error"] or "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_read_sensitive_path(self, tmp_path, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": ".ssh/secret.pdf"})
        assert result["success"] is False
        assert "sensitive" in result["error"]


class TestPDFReaderOps:
    @pytest.mark.asyncio
    async def test_extract_tables_heuristic(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "extract_tables"})
        assert result["success"] is True
        assert result["pages_scanned"] == 2

    @pytest.mark.asyncio
    async def test_extract_links(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "extract_links"})
        assert result["success"] is True
        assert "link_count" in result

    @pytest.mark.asyncio
    async def test_search_found(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "search", "query": "content"})
        assert result["success"] is True
        assert result["match_count"] >= 1

    @pytest.mark.asyncio
    async def test_search_missing_query(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "search"})
        assert result["success"] is False
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_page_info(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "page_info"})
        assert result["success"] is True
        assert result["total_pages"] == 2
        assert result["pages"][0]["page_number"] == 1
        assert result["pages"][0]["width"] > 0

    @pytest.mark.asyncio
    async def test_outline(self, pdf: str, tmp_path, tool: PDFReaderTool) -> None:
        path = tmp_path / "toc.pdf"
        _make_pdf(path, with_outline=True)
        result = await tool.execute({"file_path": str(path), "operation": "outline"})
        assert result["success"] is True
        assert result["bookmark_count"] == 2

    @pytest.mark.asyncio
    async def test_metadata(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "metadata"})
        assert result["success"] is True
        assert result["total_pages"] == 2
        assert result["total_characters"] > 0


class TestPDFReaderFileOps:
    @pytest.mark.asyncio
    async def test_split(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "split"})
        assert result["success"] is True
        assert result["page_count"] == 2
        assert len(result["files"]) == 2

    @pytest.mark.asyncio
    async def test_merge(self, pdf: str, tmp_path, tool: PDFReaderTool) -> None:
        second = tmp_path / "second.pdf"
        _make_pdf(second, pages=1)
        result = await tool.execute(
            {"file_path": pdf, "operation": "merge", "merge_files": [pdf, str(second)]}
        )
        assert result["success"] is True
        assert result["total_pages"] == 3
        assert result["output_path"].endswith("merged.pdf")

    @pytest.mark.asyncio
    async def test_merge_missing_files(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "merge"})
        assert result["success"] is False
        assert "merge_files" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_pages(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "extract_pages", "pages": [1]})
        assert result["success"] is True
        assert result["pages_extracted"] == 1

    @pytest.mark.asyncio
    async def test_convert_to_images(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "convert_to_images"})
        assert result["success"] is True
        assert result["page_count"] == 2
        assert result["files"][0]["path"].endswith(".png")

    @pytest.mark.asyncio
    async def test_extract_images(self, pdf: str, tool: PDFReaderTool) -> None:
        result = await tool.execute({"file_path": pdf, "operation": "extract_images"})
        assert result["success"] is True
        assert "image_count" in result


class TestPDFResearch:
    @pytest.mark.asyncio
    async def test_structure(self, pdf: str, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = PDFStructureTool()
        result = await tool.execute({"file_path": pdf})
        assert result["success"] is True
        assert result["data"]["page_count"] == 2
        assert result["data"]["sections"]

    @pytest.mark.asyncio
    async def test_structure_missing_file(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = PDFStructureTool()
        result = await tool.execute({"file_path": "nope.pdf"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_citations(self, pdf: str, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = CitationExtractorTool()
        result = await tool.execute({"file_path": pdf})
        assert result["success"] is True
        assert result["count"] >= 1
        assert result["citations"][0]["marker"] in ("[1]", "")


class TestLLMTools:
    @pytest.mark.asyncio
    async def test_summarizer_with_provider(
        self, pdf: str, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.core.message import LLMResponse

        class _FakeProvider:
            async def complete(self, messages, **kwargs):
                assert messages[0]["role"] == "user"
                assert kwargs.get("max_tokens") == _SECTION_SUMMARY_MAX_TOKENS
                return LLMResponse(content="Structured summary of the paper.")

        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = SectionSummarizerTool(provider=_FakeProvider())
        result = await tool.execute({"file_path": pdf, "section_title": "Method"})
        assert result["success"] is True
        assert "Structured summary" in result["summary"]
        assert result["section_title"] == "Method"

    @pytest.mark.asyncio
    async def test_summarizer_empty_text(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.core.message import LLMResponse

        class _FakeProvider:
            async def complete(self, messages, **kwargs):
                return LLMResponse(content="unused")

        import fitz

        blank = tmp_path / "blank.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(str(blank))
        doc.close()
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = SectionSummarizerTool(provider=_FakeProvider())
        result = await tool.execute({"file_path": str(blank)})
        assert result["success"] is False
        assert "No extractable text" in result["error"]

    @pytest.mark.asyncio
    async def test_translate_text_mode(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.core.message import LLMResponse

        class _FakeProvider:
            async def complete(self, messages, **kwargs):
                assert kwargs.get("max_tokens") == _TRANSLATE_MAX_TOKENS
                return LLMResponse(content="Bonjour le monde")

        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = PDFTranslateTool(provider=_FakeProvider())
        result = await tool.execute({"text": "Hello world", "target_lang": "fr"})
        assert result["success"] is True
        assert result["translated_text"] == "Bonjour le monde"
        assert result["target_lang"] == "fr"

    @pytest.mark.asyncio
    async def test_translate_no_source(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.core.message import LLMResponse

        class _FakeProvider:
            async def complete(self, messages, **kwargs):
                return LLMResponse(content="x")

        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = PDFTranslateTool(provider=_FakeProvider())
        result = await tool.execute({})
        assert result["success"] is False
        assert "No text" in result["error"]

    @pytest.mark.asyncio
    async def test_translate_bad_bbox(
        self, pdf: str, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.core.message import LLMResponse

        class _FakeProvider:
            async def complete(self, messages, **kwargs):
                return LLMResponse(content="x")

        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        tool = PDFTranslateTool(provider=_FakeProvider())
        result = await tool.execute({"file_path": pdf, "page": 1, "bbox": [1, 2, 3]})
        assert result["success"] is False
        assert "bbox" in result["error"]


class TestNoLLMProvider:
    @pytest.mark.asyncio
    async def test_summarizer_lazy_provider_none(
        self, pdf: str, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr("bandi.tools.doc.pdf_research._lazy_provider", lambda p: None)
        tool = SectionSummarizerTool(provider=None)
        result = await tool.execute({"file_path": pdf})
        assert result["success"] is False
        assert "LLM provider" in result["error"]

    @pytest.mark.asyncio
    async def test_translate_lazy_provider_none(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr("bandi.tools.doc.pdf_research._lazy_provider", lambda p: None)
        tool = PDFTranslateTool(provider=None)
        result = await tool.execute({"text": "hello"})
        assert result["success"] is False
        assert "LLM provider" in result["error"]
