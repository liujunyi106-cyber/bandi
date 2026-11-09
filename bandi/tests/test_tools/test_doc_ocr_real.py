# 功能：ImageOCRTool 真实 PaddleOCR 集成测试（依赖 paddleocr + 模型已缓存）
# 环境缺 paddleocr 时跳过；装了就真实推理（enable_mkldnn=False 规避 paddlepaddle 3.3.1 onednn bug）

from __future__ import annotations

import pytest

import bandi.tools.doc.image_ocr as mod
from bandi.tools.doc.image_ocr import ImageOCRTool

pytestmark = pytest.mark.skipif(not mod.PADDLEOCR_AVAILABLE, reason="paddleocr not installed")


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> ImageOCRTool:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return ImageOCRTool()


def _make_text_image(path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (500, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "HELLO BANDI OCR", fill="black")
    img.save(str(path))


class TestImageOCRReal:
    @pytest.mark.asyncio
    async def test_real_ocr_extracts_english_text(self, tool, tmp_path) -> None:
        img = tmp_path / "text.png"
        _make_text_image(img)
        r = await tool.execute({"file_path": str(img)})
        assert r["success"] is True, r
        assert r["line_count"] >= 1
        assert "HELLO" in r["text"].replace(" ", "").replace("\n", "")
        assert r["total_characters"] > 0

    @pytest.mark.asyncio
    async def test_real_ocr_confidence_and_boxes(self, tool, tmp_path) -> None:
        img = tmp_path / "text.png"
        _make_text_image(img)
        r = await tool.execute(
            {"file_path": str(img), "include_confidence": True, "include_boxes": True}
        )
        assert r["success"] is True, r
        assert 0.0 <= r["avg_confidence"] <= 1.0
        assert r["lines"][0]["confidence"] > 0.5
        box = r["lines"][0]["box"]
        assert len(box) == 4
        assert all("x" in p and "y" in p for p in box)

    @pytest.mark.asyncio
    async def test_real_ocr_min_confidence_filters(self, tool, tmp_path) -> None:
        img = tmp_path / "text.png"
        _make_text_image(img)
        r = await tool.execute({"file_path": str(img), "min_confidence": 0.999})
        assert r["success"] is True
        assert r["line_count"] == 0
        assert r.get("filtered_low_confidence", 0) >= 1

    @pytest.mark.asyncio
    async def test_real_ocr_en_language(self, tool, tmp_path) -> None:
        img = tmp_path / "text.png"
        _make_text_image(img)
        r = await tool.execute({"file_path": str(img), "language": "en"})
        assert r["success"] is True, r
        assert r["line_count"] >= 1
