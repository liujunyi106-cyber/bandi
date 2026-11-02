# 功能：视觉 + 分类 + 知识库工具测试 — ImageOCR/DocClassifier/KnowledgeSearch

from __future__ import annotations

import pytest

from bandi.core.message import LLMResponse
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.tools.doc.doc_classifier import DocClassifierTool
from bandi.tools.doc.image_ocr import ImageOCRTool
from bandi.tools.doc.knowledge_search import KnowledgeSearchTool


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


class TestImageOCR:
    def _fake_ocr(self):
        class FakeOCR:
            def predict(self, path):
                return [
                    {
                        "res": {
                            "rec_texts": ["Hello world", "垃圾行"],
                            "rec_scores": [0.98, 0.2],
                            "rec_polys": [
                                [[0, 0], [10, 0], [10, 10], [0, 10]],
                                [[0, 20], [10, 20], [10, 30], [0, 30]],
                            ],
                        }
                    }
                ]

        return FakeOCR()

    @pytest.mark.asyncio
    async def test_paddleocr_not_installed(self, tool, tmp_path) -> None:
        import bandi.tools.doc.image_ocr as mod

        monkeypatch_ocr = pytest.MonkeyPatch()
        monkeypatch_ocr.setattr(mod, "PADDLEOCR_AVAILABLE", False)
        f = tmp_path / "a.png"
        f.write_bytes(b"fake")
        try:
            r = await ImageOCRTool().execute({"file_path": str(f)})
            assert r["success"] is False
            assert "PaddleOCR" in r["error"]
        finally:
            monkeypatch_ocr.undo()

    @pytest.mark.asyncio
    async def test_ocr_extracts_lines_with_filters(
        self, tool, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bandi.tools.doc.image_ocr as mod

        monkeypatch.setattr(mod, "PADDLEOCR_AVAILABLE", True)
        monkeypatch.setattr(
            mod, "get_ocr_instance", lambda lang="ch", use_gpu=False: self._fake_ocr()
        )
        f = tmp_path / "a.png"
        f.write_bytes(b"fake")
        tool_ = ImageOCRTool()
        r = await tool_.execute({"file_path": str(f), "include_boxes": True})
        assert r["success"] is True
        assert r["text"] == "Hello world"
        assert r["line_count"] == 1
        assert r["avg_confidence"] == 0.98
        assert r["filtered_low_confidence"] == 1
        assert r["lines"][0]["box"][0]["x"] == 0

    @pytest.mark.asyncio
    async def test_ocr_errors(self, tool, tmp_path) -> None:
        import bandi.tools.doc.image_ocr as mod

        monkeypatch_ocr = pytest.MonkeyPatch()
        monkeypatch_ocr.setattr(mod, "PADDLEOCR_AVAILABLE", True)
        try:
            tool_ = ImageOCRTool()
            assert (await tool_.execute({"file_path": ""}))["success"] is False
            assert (await tool_.execute({"file_path": "missing.png"}))["success"] is False
            bad = tmp_path / "a.txt"
            bad.write_text("x", encoding="utf-8")
            assert (await tool_.execute({"file_path": str(bad)}))["success"] is False
        finally:
            monkeypatch_ocr.undo()


class TestDocClassifier:
    @pytest.mark.asyncio
    async def test_rules_classify_invoice(self, tool) -> None:
        r = await DocClassifierTool().execute(
            {
                "method": "rules",
                "text": "INVOICE No. 001\nSubtotal: 100.00\nAmount Due: 120.00\nInvoice Date: 2026-01-01",
            }
        )
        assert r["success"] is True
        assert r["category"] == "invoice"
        assert r["confidence"] > 0.5
        assert r["method_used"] == "rules"

    @pytest.mark.asyncio
    async def test_rules_no_match_returns_other(self, tool) -> None:
        r = await DocClassifierTool().execute(
            {"method": "rules", "text": "量子色动力学是一门基础学科"}
        )
        assert r["success"] is True
        assert r["category"] == "other"

    @pytest.mark.asyncio
    async def test_hybrid_falls_back_to_rules_without_llm(
        self, tool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bandi.tools.doc.doc_classifier as mod

        monkeypatch.setattr(mod, "_lazy_provider", lambda provider: None)
        text = "INVOICE No. 001\nAmount Due: 100.00"
        r = await DocClassifierTool().execute(
            {"method": "hybrid", "text": text, "confidence_threshold": 0.99}
        )
        assert r["success"] is True
        assert r["category"] == "invoice"
        assert r["method_used"] == "rules (fallback)"
        assert "warning" in r

    @pytest.mark.asyncio
    async def test_llm_method_with_provider(self, tool) -> None:
        class FakeProvider:
            async def complete(self, messages, tools=None, **kwargs):
                return LLMResponse(content="invoice")

        r = await DocClassifierTool(provider=FakeProvider()).execute(
            {"method": "llm", "text": "some document"}
        )
        assert r["success"] is True
        assert r["category"] == "invoice"
        assert r["confidence"] == 0.85
        assert r["method_used"] == "llm"

    @pytest.mark.asyncio
    async def test_llm_normalizes_unknown_category(self, tool) -> None:
        class FakeProvider:
            async def complete(self, messages, tools=None, **kwargs):
                return LLMResponse(content="this is definitely a contract document")

        r = await DocClassifierTool(provider=FakeProvider()).execute(
            {"method": "llm", "text": "x", "custom_categories": ["invoice", "contract"]}
        )
        assert r["category"] == "contract"

    @pytest.mark.asyncio
    async def test_file_path_and_errors(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("MEMORANDUM\nTo: All\nSubject: test", encoding="utf-8")
        tool_ = DocClassifierTool()
        r = await tool_.execute({"method": "rules", "file_path": str(f)})
        assert r["success"] is True
        assert r["category"] == "memo"
        assert (await tool_.execute({"method": "rules"}))["success"] is False
        assert (await tool_.execute({"method": "rules", "file_path": "missing.txt"}))[
            "success"
        ] is False
        r2 = await tool_.execute({"method": "rules", "text": "   "})
        assert r2["success"] is True and r2["category"] == "other"


class TestKnowledgeSearch:
    def _memory(self) -> LongTermMemory:
        mem = LongTermMemory()
        mem.store(MemoryItem("k1", "Bandi ontology pipeline stores semantic relations"))
        mem.store(MemoryItem("k2", "PaddleOCR extracts Chinese text from images"))
        mem.store(MemoryItem("k3", "FTS5 supports BM25 ranking in SQLite"))
        return mem

    @pytest.mark.asyncio
    async def test_catalog_lists_items(self, tool) -> None:
        r = await KnowledgeSearchTool(memory=self._memory()).execute({"action": "catalog"})
        assert r["success"] is True
        assert r["total"] == 3
        ids = {doc["item_id"] for doc in r["documents"]}
        assert ids == {"k1", "k2", "k3"}

    @pytest.mark.asyncio
    async def test_search_fts_hits(self, tool) -> None:
        r = await KnowledgeSearchTool(memory=self._memory()).execute(
            {"action": "search", "query": "BM25", "limit": 5}
        )
        assert r["success"] is True
        assert r["total"] >= 1
        ids = {hit["item_id"] for hit in r["results"]}
        assert "k3" in ids

    @pytest.mark.asyncio
    async def test_search_no_match(self, tool) -> None:
        r = await KnowledgeSearchTool(memory=self._memory()).execute(
            {"action": "search", "query": "zzznotfound"}
        )
        assert r["success"] is True
        assert r["total"] == 0

    @pytest.mark.asyncio
    async def test_read_window(self, tool) -> None:
        tool_ = KnowledgeSearchTool(memory=self._memory())
        r = await tool_.execute({"action": "read", "item_id": "k1"})
        assert r["success"] is True
        assert r["text"] == "Bandi ontology pipeline stores semantic relations"
        assert r["total_chars"] > 0 and r["truncated"] is False
        r2 = await tool_.execute(
            {"action": "read", "item_id": "k1", "start_offset": 0, "end_offset": 5}
        )
        assert r2["text"] == "Bandi"

    @pytest.mark.asyncio
    async def test_read_errors(self, tool) -> None:
        tool_ = KnowledgeSearchTool(memory=self._memory())
        assert (await tool_.execute({"action": "read", "item_id": "missing"}))["success"] is False
        assert (await tool_.execute({"action": "read"}))["success"] is False
        assert (await tool_.execute({"action": "search"}))["success"] is False
        assert (await tool_.execute({"action": "nope"}))["success"] is False
