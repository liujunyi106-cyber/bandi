# 功能：ImageOCRTool — 图片 OCR 文本提取（PaddleOCR 惰性，未装时友好报错）

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

PADDLEOCR_AVAILABLE = False

try:
    from paddleocr import PaddleOCR

    PADDLEOCR_AVAILABLE = True
except ImportError:
    pass

_SUPPORTED_FORMATS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}

_LANG_ALIASES = {"ch_en": "ch"}

_INSTALL_HINT = (
    "PaddleOCR is not installed. Install with: pip install paddlepaddle paddleocr "
    "(or paddlepaddle-gpu paddleocr for GPU)"
)


def _normalize_lang(lang: str) -> str:
    return _LANG_ALIASES.get(lang, lang)


_ocr_cache: dict[tuple[str, bool], object] = {}


def get_ocr_instance(lang: str = "ch", use_gpu: bool = False) -> object:
    """获取/缓存 PaddleOCR 实例（初始化昂贵，缓存复用；PaddleOCR 3.x 参数）。"""
    # lang="en" 请求拿到 lang="ch" 实例（语言参数语义失效）
    key = (_normalize_lang(lang), use_gpu)
    instance = _ocr_cache.get(key)
    if instance is not None:
        return instance
    kwargs: dict[str, Any] = {"lang": _normalize_lang(lang)}
    if use_gpu:
        kwargs["device"] = "gpu"
    else:
        kwargs["device"] = "cpu"
        kwargs["enable_mkldnn"] = False  # 规避 paddlepaddle 3.x onednn PIR 执行器 bug
    instance = PaddleOCR(**kwargs)
    _ocr_cache[key] = instance
    return instance


class ImageOCRTool(FsTool):
    name: str = "image_ocr"
    # 安全（③⁻）：OCR 文本外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Extract text from images using OCR. Supports Chinese and English text with "
        "confidence scores and optional bounding boxes (PNG/JPG/JPEG/BMP/TIFF/WEBP). "
        "Requires PaddleOCR; returns an install hint when unavailable."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the image file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "language": {
                "type": "string",
                "enum": ["ch", "en", "ch_en"],
                "description": "OCR language: ch (Chinese+English, default), en, ch_en",
            },
            "include_confidence": {
                "type": "boolean",
                "description": "Include per-line confidence (default true)",
            },
            "include_boxes": {
                "type": "boolean",
                "description": "Include bounding box coordinates (default false)",
            },
            "min_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Minimum confidence 0-1 to keep a line (default 0.5)",
            },
            "use_gpu": {"type": "boolean", "description": "Use GPU for OCR (default false)"},
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        if not PADDLEOCR_AVAILABLE:
            return {"success": False, "error": _INSTALL_HINT}
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file():
                return {"success": False, "error": f"image file not found: {path_str}"}
            if path.suffix.lower() not in _SUPPORTED_FORMATS:
                return {
                    "success": False,
                    "error": f"unsupported image format: {path.suffix}. Supported: {', '.join(sorted(_SUPPORTED_FORMATS))}",
                }
            return await self._run_ocr(
                path,
                language=params.get("language", "ch"),
                include_confidence=bool(params.get("include_confidence", True)),
                include_boxes=bool(params.get("include_boxes", False)),
                min_confidence=float(params.get("min_confidence", 0.5)),
                use_gpu=bool(params.get("use_gpu", False)),
            )
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    async def _run_ocr(
        self,
        path: Path,
        *,
        language: str,
        include_confidence: bool,
        include_boxes: bool,
        min_confidence: float,
        use_gpu: bool,
    ) -> dict[str, Any]:
        try:
            ocr = get_ocr_instance(lang=language, use_gpu=use_gpu)
            result = ocr.predict(str(path))
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"OCR processing failed: {e}"}

        if not result:
            return {
                "success": True,
                "text": "",
                "lines": [],
                "line_count": 0,
                "avg_confidence": 0.0,
                "warning": "No text detected in the image.",
            }

        parsed = result[0]
        data = parsed.json if hasattr(parsed, "json") else parsed
        if not isinstance(data, dict):
            return {"success": False, "error": "unexpected OCR result format"}
        res = data.get("res", data)
        texts = res.get("rec_texts") or []
        scores = res.get("rec_scores") or []
        polys = res.get("rec_polys") or res.get("dt_polys") or []

        lines: list[dict[str, Any]] = []
        text_parts: list[str] = []
        total_confidence = 0.0
        filtered_count = 0

        for line in zip(texts, scores, polys):
            text, confidence, box = line
            if confidence < min_confidence:
                filtered_count += 1
                continue
            line_data: dict[str, Any] = {"text": text}
            if include_confidence:
                line_data["confidence"] = round(confidence, 4)
            if include_boxes:
                line_data["box"] = [{"x": point[0], "y": point[1]} for point in box]
            lines.append(line_data)
            text_parts.append(text)
            total_confidence += confidence

        output: dict[str, Any] = {
            "success": True,
            "text": "\n".join(text_parts),
            "lines": lines,
            "line_count": len(lines),
            "total_characters": len("".join(text_parts)),
        }
        if include_confidence:
            output["avg_confidence"] = round(total_confidence / len(lines), 4) if lines else 0.0
        if filtered_count > 0:
            output["filtered_low_confidence"] = filtered_count
        return output
