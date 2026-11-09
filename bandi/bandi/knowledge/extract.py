# 功能：资料库文本抽取——按节点类型从原始字节提取纯文本供 ingest（失败返回空串，不阻断上传）。

from __future__ import annotations

import io

_TEXT_KINDS = {"md", "csv", "html"}


def extract_text(kind: str, data: bytes) -> str:
    # 功能：分发到对应解析器；任何解析失败返回空串（调用方决定是否跳过 ingest）。
    try:
        if kind in _TEXT_KINDS:
            return data.decode("utf-8-sig", errors="replace")
        if kind == "pdf":
            return _pdf_text(data)
        if kind == "docx":
            return _docx_text(data)
        if kind == "xlsx":
            return _xlsx_text(data)
        if kind == "pptx":
            return _pptx_text(data)
    except Exception:  # noqa: BLE001 - 文本抽取失败不影响文件保存
        return ""
    return ""


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _docx_text(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    chunks: list[str] = []
    for sheet in workbook.worksheets:
        chunks.append(f"# {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(value) for value in row if value is not None]
            if cells:
                chunks.append(" | ".join(cells))
    workbook.close()
    return "\n".join(chunks)


def _pptx_text(data: bytes) -> str:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    chunks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        chunks.append(f"# 第 {index} 页")
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and str(text).strip():
                chunks.append(str(text))
    return "\n".join(chunks)
