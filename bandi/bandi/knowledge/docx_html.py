# 功能：docx 纯 Python 预览——段落/标题/列表/表格转安全 HTML，不依赖 LibreOffice。

from __future__ import annotations

import io
from html import escape

MAX_RENDER_CHARS = 2_000_000
_HEADING_TAGS = {f"heading {index}": f"h{index}" for index in range(1, 7)}
_STYLE = (
    "body{margin:24px auto;max-width:820px;padding:0 24px;color:#18181b;"
    "font:15px/1.75 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;}"
    "h1,h2,h3,h4,h5,h6{line-height:1.35;margin:1.4em 0 .6em;}"
    "h1{font-size:26px}h2{font-size:22px}h3{font-size:18px}h4{font-size:16px}"
    "p{margin:.5em 0}ul,ol{padding-left:1.5em;margin:.5em 0}"
    "table{border-collapse:collapse;width:100%;margin:1em 0;font-size:14px}"
    "td,th{border:1px solid #e4e4e7;padding:6px 10px;vertical-align:top;text-align:left}"
    "strong{font-weight:600}em{font-style:italic}"
)


def render_docx_html(data: bytes) -> str:
    # 功能：把 docx 字节渲染成完整 HTML 文档；解析失败抛 ValueError 由调用方降级。
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    body = document.element.body
    parts: list[str] = []
    used = 0
    list_items: list[str] = []
    list_tag = ""

    def flush_list() -> None:
        nonlocal list_items, list_tag
        if list_items:
            parts.append(f"<{list_tag}>{''.join(list_items)}</{list_tag}>")
        list_items = []
        list_tag = ""

    for child in body.iterchildren():
        tag = str(child.tag).rsplit("}", 1)[-1]
        if tag not in {"p", "tbl"}:
            continue
        if tag == "tbl":
            flush_list()
            table = Table(child, document)
            rows = []
            for row in table.rows:
                cells = "".join(
                    f"<td>{_paragraph_inline(cell.paragraphs[0]) if cell.paragraphs else ''}</td>"
                    for cell in row.cells
                )
                rows.append(f"<tr>{cells}</tr>")
            parts.append(f"<table>{''.join(rows)}</table>")
            used += sum(len(row) for row in rows)
            continue
        paragraph = Paragraph(child, document)
        text = (paragraph.text or "").strip()
        if not text:
            flush_list()
            continue
        style = (paragraph.style.name if paragraph.style is not None else "").strip().lower()
        heading = _HEADING_TAGS.get(style)
        if heading:
            flush_list()
            parts.append(f"<{heading}>{_paragraph_inline(paragraph)}</{heading}>")
        elif style.startswith("title"):
            flush_list()
            parts.append(f"<h1>{_paragraph_inline(paragraph)}</h1>")
        elif _list_tag_for(style):
            tag = _list_tag_for(style)
            if list_tag != tag:
                flush_list()
                list_tag = tag
            list_items.append(f"<li>{_paragraph_inline(paragraph)}</li>")
        else:
            flush_list()
            parts.append(f"<p>{_paragraph_inline(paragraph)}</p>")
        used += len(text)
        if used > MAX_RENDER_CHARS:
            flush_list()
            parts.append("<p>（内容过长，已截断；请下载原文件查看）</p>")
            break
    flush_list()
    html = "".join(parts)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{_STYLE}</style></head><body>{html}</body></html>"
    )


def _list_tag_for(style: str) -> str:
    if "list bullet" in style or "list paragraph" in style:
        return "ul"
    if "list number" in style:
        return "ol"
    return ""


def _paragraph_inline(paragraph: object) -> str:
    # 功能：段落内联格式只保留粗体/斜体，其余文本全部转义。
    runs = getattr(paragraph, "runs", None) or []
    if not runs:
        return escape(str(getattr(paragraph, "text", "") or ""))
    pieces: list[str] = []
    for run in runs:
        text = escape(str(getattr(run, "text", "") or ""))
        if not text:
            continue
        if getattr(run, "bold", False):
            text = f"<strong>{text}</strong>"
        if getattr(run, "italic", False):
            text = f"<em>{text}</em>"
        pieces.append(text)
    return "".join(pieces)
