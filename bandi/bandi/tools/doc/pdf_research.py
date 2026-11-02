# 功能：pdf_research — 学术 PDF 4 工具（结构/引用无 LLM + 摘要/翻译有 LLM）

from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.doc import pdf_research_core as core
from bandi.tools.doc._doc_utils import lazy_provider as _lazy_provider
from bandi.tools.fs_tool import FsTool

_SUMMARY_CHAR_BUDGET = 16_000
_SECTION_SUMMARY_MAX_TOKENS = 1200
_TRANSLATE_MAX_TOKENS = 2000


class PDFStructureTool(FsTool):
    name: str = "pdf_structure"
    # 安全（③⁻）：PDF 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Analyze a PDF's structure: page count, title, outline/bookmarks, heuristic section "
        "headings, and detected figures/tables with page numbers. Use before summarizing or "
        "navigating an academic paper."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the PDF file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file() or path.suffix.lower() != ".pdf":
                return {"success": False, "error": f"not a PDF file: {path_str}"}
            structure = core.extract_structure(str(path))
            return {"success": True, "data": structure}
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}


class CitationExtractorTool(FsTool):
    name: str = "citation_extractor"
    # 安全（③⁻）：PDF 引用内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Extract the reference list from an academic PDF. Returns each entry with its marker "
        "(e.g. [1]), full text, and any DOI/URL detected."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the PDF file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "max_items": {
                "type": "integer",
                "description": "Max citations to extract (default 200)",
            },
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file() or path.suffix.lower() != ".pdf":
                return {"success": False, "error": f"not a PDF file: {path_str}"}
            citations = core.extract_citations(str(path), max_items=params.get("max_items", 200))
            return {"success": True, "citations": citations, "count": len(citations)}
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}


class SectionSummarizerTool(FsTool):
    name: str = "section_summarizer"
    # 安全（③⁻）：PDF 摘要内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Summarize a PDF page range (or whole paper) into a concise, structured summary. "
        "Provide start_page/end_page to target a specific section. Uses the LLM provider."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the PDF file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "start_page": {"type": "integer", "description": "1-based start page"},
            "end_page": {"type": "integer", "description": "1-based end page inclusive"},
            "section_title": {"type": "string", "description": "Optional section title hint"},
            "target_lang": {
                "type": "string",
                "description": "Optional target language for summary",
            },
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    risk_level: str = "low"

    def __init__(self, provider: Any | None = None) -> None:
        super().__init__()
        self._provider = provider

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        provider = _lazy_provider(self._provider)
        if provider is None:
            return {"success": False, "error": "LLM provider not available"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file() or path.suffix.lower() != ".pdf":
                return {"success": False, "error": f"not a PDF file: {path_str}"}
            text = core.extract_page_text(
                str(path), params.get("start_page"), params.get("end_page")
            )
            if not text.strip():
                return {
                    "success": False,
                    "error": "No extractable text (the PDF may be scanned; try OCR).",
                }
            prompt = _build_summary_prompt(
                text[:_SUMMARY_CHAR_BUDGET],
                section_title=params.get("section_title"),
                target_lang=params.get("target_lang"),
            )
            from bandi.llm.accounted import accounted_complete

            response = await accounted_complete(
                provider,
                [{"role": "user", "content": prompt}],
                label="pdf_summary",
                session_id="pdf_summary",
                max_tokens=_SECTION_SUMMARY_MAX_TOKENS,
            )
            summary = (response.content or "").strip()
            return {
                "success": True,
                "summary": summary,
                "section_title": params.get("section_title"),
            }
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}


class PDFTranslateTool(FsTool):
    name: str = "pdf_translate"
    # 安全（③⁻）：PDF 翻译源文本外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Translate text into a target language. Either pass 'text', or pass 'file_path' + "
        "'page' + 'bbox' to translate a region of a PDF page. Uses the LLM provider."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to translate"},
            "file_path": {"type": "string", "description": "Path to the PDF file (region mode)"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "page": {"type": "integer", "description": "1-based page number (region mode)"},
            "bbox": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Bounding box [x0, y0, x1, y1] in PDF points (region mode)",
            },
            "target_lang": {"type": "string", "description": "Target language (default en)"},
        },
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    risk_level: str = "low"

    def __init__(self, provider: Any | None = None) -> None:
        super().__init__()
        self._provider = provider

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        provider = _lazy_provider(self._provider)
        if provider is None:
            return {"success": False, "error": "LLM provider not available"}
        target_lang = params.get("target_lang") or "en"
        source = params.get("text") or ""
        try:
            if not source:
                path_str = params.get("file_path", "")
                page = params.get("page")
                bbox = params.get("bbox")
                if not (path_str and page and bbox):
                    return {"success": False, "error": "No text to translate."}
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                    return {"success": False, "error": "bbox must be [x0, y0, x1, y1]."}
                path = self._resolve_safe_path(path_str)
                if self._is_sensitive(path):
                    return {"success": False, "error": f"sensitive path rejected: {path_str}"}
                source = core.extract_region_text(
                    str(path), int(page), tuple(float(v) for v in bbox)
                )
            if not source.strip():
                return {"success": False, "error": "No text to translate."}
            prompt = _build_translate_prompt(source, target_lang)
            from bandi.llm.accounted import accounted_complete

            response = await accounted_complete(
                provider,
                [{"role": "user", "content": prompt}],
                label="pdf_translate",
                session_id="pdf_translate",
                max_tokens=_TRANSLATE_MAX_TOKENS,
            )
            translated = (response.content or "").strip()
            return {
                "success": True,
                "source_text": source,
                "translated_text": translated,
                "target_lang": target_lang,
            }
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}


def _build_summary_prompt(
    text: str, *, section_title: str | None = None, target_lang: str | None = None
) -> str:
    section_hint = f" Focus on the section '{section_title}'." if section_title else ""
    lang_hint = f" Write the summary in {target_lang}." if target_lang else ""
    return (
        "You are a research assistant. Summarize the following paper text as a structured "
        "summary: key ideas, methods, and main findings. Use short paragraphs or bullet points. "
        "Do not invent facts not present in the text."
        f"{section_hint}{lang_hint}\n\nText:\n{text}"
    )


def _build_translate_prompt(text: str, target_lang: str) -> str:
    return (
        f"Translate the following text into {target_lang}. Preserve technical terms, "
        "mathematical notation, and formatting. Return only the translation.\n\n"
        f"Text:\n{text}"
    )
