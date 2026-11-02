# 功能：gen 渲染纯函数——markdown/slides 到 markdown/html/docx/pdf/pptx/xlsx 的转换层
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import pymupdf as fitz

_PAGE_W: float = 595.0
_PAGE_H: float = 842.0
_MARGIN: float = 48.0

_LINE_HEADING: dict[int, float] = {1: 22.0, 2: 17.0, 3: 14.0}

_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _plain(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", text)
    return text


def parse_md_blocks(md: str) -> list[dict[str, Any]]:
    """功能：行级 markdown 解析——产出结构块（heading/para/bullet/code/table/quote/hr）。"""
    blocks: list[dict[str, Any]] = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped == "":
            i += 1
            continue
        if re.fullmatch(r"---+", stripped):
            blocks.append({"type": "hr"})
            i += 1
            continue
        h = re.fullmatch(r"(#{1,6})\s+(.*)", stripped)
        if h:
            blocks.append({"type": "heading", "level": len(h.group(1)), "text": _plain(h.group(2))})
            i += 1
            continue
        if stripped.startswith("```"):
            j = i + 1
            buf: list[str] = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            blocks.append({"type": "code", "text": "\n".join(buf)})
            i = j + 1
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            blocks.append({"type": "bullet", "text": _plain(re.sub(r"^\s*[-*+]\s+", "", line))})
            i += 1
            continue
        if re.match(r"^\s*\d+[.、)]\s+", line):
            blocks.append({"type": "bullet", "text": _plain(re.sub(r"^\s*\d+[.、)]\s+", "", line))})
            i += 1
            continue
        if stripped.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append({"type": "quote", "text": " ".join(quote)})
            continue
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and len(lines[i + 1].split("|")) >= 3
        ):
            headers = _split_table_row(lines[i])
            j = i + 2
            rows: list[list[str]] = []
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(_split_table_row(lines[j]))
                j += 1
            blocks.append({"type": "table", "headers": headers, "rows": rows})
            i = j
            continue
        para: list[str] = [line]
        while (
            i + 1 < len(lines)
            and lines[i + 1].strip()
            and not re.match(r"^\s*[#>\-*+|\d.]", lines[i + 1])
            and not lines[i + 1].lstrip().startswith("```")
        ):
            para.append(lines[i + 1])
            i += 1
        blocks.append({"type": "para", "text": _plain(" ".join(para))})
        i += 1
    return blocks


def _split_table_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [_plain(c) for c in cells if not re.fullmatch(r":?-{2,}:?", c)]


def format_from_path(path: Path, exts: dict[str, str]) -> str | None:
    """功能：按输出文件扩展名推断渲染格式（未识别返回 None）。"""
    suf = path.suffix.lower()
    for fmt, ext in exts.items():
        if ext == suf:
            return fmt
    return None


def render_document(md: str, fmt: str) -> bytes:
    """功能：markdown 文档渲染为指定格式 bytes（markdown/html/docx/pdf）。"""
    fmt = fmt.lower()
    if fmt == "markdown":
        return md.encode("utf-8")
    blocks = parse_md_blocks(md)
    if fmt == "html":
        return _blocks_to_html(blocks).encode("utf-8")
    if fmt == "docx":
        return _blocks_to_docx(blocks)
    if fmt == "pdf":
        return _blocks_to_pdf(blocks)
    raise ValueError(f"unsupported document format: {fmt}")


def render_slides(slides: list[dict[str, Any]], fmt: str) -> bytes:
    """功能：slides 对象列表渲染为指定格式 bytes（markdown/html/pptx/pdf）。"""
    fmt = fmt.lower()
    if not slides:
        raise ValueError("slides must contain at least one slide")
    if fmt == "markdown":
        parts: list[str] = []
        for i, slide in enumerate(slides):
            title, body, takeaway = _slide_fields(slide)
            if i:
                parts.append("---")
            parts.append(f"# {title}" if title else "# 幻灯片")
            if body:
                parts.append(body)
            if takeaway:
                parts.append(f"> **结论：** {takeaway}")
        return ("\n\n".join(parts) + "\n").encode("utf-8")
    if fmt == "html":
        sections: list[str] = []
        for slide in slides:
            title, body, takeaway = _slide_fields(slide)
            s = f"<section>\n<h1>{html.escape(title)}</h1>\n"
            if body:
                s += f"<p>{html.escape(body).replace(chr(10), '<br>')}</p>\n"
            if takeaway:
                s += f"<p class='takeaway'>{html.escape(takeaway)}</p>\n"
            s += "</section>"
            sections.append(s)
        html_doc = f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Slides</title><style>{_SLIDES_CSS}</style></head><body>{''.join(sections)}</body></html>"
        return html_doc.encode("utf-8")
    if fmt == "pptx":
        return _slides_to_pptx(slides)
    if fmt == "pdf":
        return _slides_to_pdf(slides)
    raise ValueError(f"unsupported slides format: {fmt}")


_SLIDES_CSS = """
body{background:#101418;color:#e6e9ee;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}
section{min-height:520px;padding:56px;margin:16px auto;max-width:900px;background:#171c23;border-radius:18px;border:1px solid #2a3240}
h1{color:#7aa2ff;font-size:34px}
p{font-size:19px;line-height:1.6}
.takeaway{color:#7ee787}
"""


def _slide_fields(slide: dict[str, Any]) -> tuple[str, str, str]:
    any_map = slide or {}
    return (
        str(any_map.get("title") or ""),
        str(any_map.get("body") or ""),
        str(any_map.get("takeaway") or ""),
    )


# ---------- HTML ----------


def _blocks_to_html(blocks: list[dict[str, Any]]) -> str:
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Document</title><style>",
        "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:40px auto;line-height:1.7;color:#222}",
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px}",
        "pre{background:#f6f8fa;padding:14px;border-radius:8px;overflow:auto}",
        "blockquote{border-left:4px solid #7aa2ff;margin:0;padding:4px 14px;color:#555}</style></head><body>",
    ]
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            level = min(int(block.get("level", 2)), 6)
            parts.append(f"<h{level}>{html.escape(block.get('text', ''))}</h{level}>")
        elif kind == "para":
            parts.append(f"<p>{html.escape(block.get('text', ''))}</p>")
        elif kind == "bullet":
            parts.append("<ul><li>{}</li></ul>".format(html.escape(block.get("text", ""))))
        elif kind == "code":
            parts.append("<pre>{}</pre>".format(html.escape(block.get("text", ""))))
        elif kind == "quote":
            parts.append("<blockquote>{}</blockquote>".format(html.escape(block.get("text", ""))))
        elif kind == "hr":
            parts.append("<hr>")
        elif kind == "table":
            head = "".join(f"<th>{html.escape(h)}</th>" for h in block.get("headers", []))
            rows = "".join(
                "<tr>{}</tr>".format("".join(f"<td>{html.escape(c)}</td>" for c in row))
                for row in block.get("rows", [])
            )
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    parts.append("</body></html>")
    return "".join(parts)


# ---------- DOCX ----------


def _rich(paragraph: Any, text: str) -> None:  # 功能：docx 段落写纯文本（粗体星号已剥）
    paragraph.add_run(text)


def _blocks_to_docx(blocks: list[dict[str, Any]]) -> bytes:
    from io import BytesIO

    from docx import Document

    doc = Document()
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            level = min(int(block.get("level", 2)), 6)
            doc.add_heading(block.get("text", ""), level=level)
        elif kind == "para":
            _rich(doc.add_paragraph(), block.get("text", ""))
        elif kind == "bullet":
            _rich(doc.add_paragraph(style="List Bullet"), block.get("text", ""))
        elif kind == "code":
            p = doc.add_paragraph()
            run = p.add_run(block.get("text", ""))
            run.font.name = "Consolas"
        elif kind == "quote":
            _rich(doc.add_paragraph(style="Intense Quote"), block.get("text", ""))
        elif kind == "hr":
            p = doc.add_paragraph()
            p.add_run("—" * 20)
        elif kind == "table":
            headers = block.get("headers", [])
            rows = block.get("rows", [])
            if headers and rows:
                table = doc.add_table(rows=1 + len(rows), cols=len(headers))
                for c, head in enumerate(headers):
                    table.rows[0].cells[c].text = head
                for r, row in enumerate(rows, start=1):
                    for c in range(len(headers)):
                        table.rows[r].cells[c].text = row[c] if c < len(row) else ""
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------- PDF ----------


class _PdfCanvas:
    """功能：PyMuPDF 手动版式画布——字号层级表达语义，灰度配色，自动折行与分页。"""

    def __init__(self) -> None:
        self.doc = fitz.open()
        self.page = self.doc.new_page(width=_PAGE_W, height=_PAGE_H)
        self.y = _MARGIN
        self._font_cache: dict[str, fitz.Font] = {}

    def _font(self, name: str) -> fitz.Font:
        if name not in self._font_cache:
            self._font_cache[name] = fitz.Font(fontname=name)
        return self._font_cache[name]

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=_PAGE_W, height=_PAGE_H)
        self.y = _MARGIN

    @staticmethod
    def _wrap(text: str, font: fitz.Font, size: float, max_w: float) -> list[str]:
        lines: list[str] = []
        current = ""
        for ch in text:
            if ch == "\n":
                if current:
                    lines.append(current)
                current = ""
                continue
            if current and font.text_length(current + ch, fontsize=size) > max_w:
                lines.append(current)
                current = ch
            else:
                current += ch
        if current:
            lines.append(current)
        return lines

    def text(
        self,
        text: str,
        size: float,
        fontname: str = "helv",
        indent: float = 0,
        line_gap: float = 1.45,
        block_gap: float = 6.0,
    ) -> None:
        if not text:
            return
        font = self._font(fontname)
        max_w = _PAGE_W - 2 * _MARGIN - indent
        tw = fitz.TextWriter(self.page.rect)
        x0 = _MARGIN + indent
        for line in self._wrap(text, font, size, max_w):
            if self.y + size * 1.25 > _PAGE_H - _MARGIN:
                tw.write_text(self.page)
                self.new_page()
                tw = fitz.TextWriter(self.page.rect)
            tw.append(fitz.Point(x0, self.y), line, font=font, fontsize=size)
            self.y += size * line_gap
        tw.write_text(self.page)
        self.y += block_gap

    def space(self, gap: float = 10.0) -> None:
        self.y += gap

    def text_pdf(self) -> bytes:
        return self.doc.tobytes()


def _blocks_to_pdf(blocks: list[dict[str, Any]]) -> bytes:
    cv = _PdfCanvas()
    for block in blocks:
        kind = block["type"]
        if kind == "hr":
            cv.space(4)
        elif kind == "heading":
            level = int(block.get("level", 2))
            size = _LINE_HEADING.get(level, 13.0)
            cv.text(block.get("text", ""), size, fontname="china-s", block_gap=8.0)
        elif kind == "para":
            cv.text(block.get("text", ""), 11, fontname="china-s")
        elif kind == "bullet":
            cv.text("- " + block.get("text", ""), 11, fontname="china-s", indent=12)
        elif kind == "code":
            cv.text(block.get("text", ""), 9, fontname="courier", indent=12)
        elif kind == "quote":
            cv.text("▍ " + block.get("text", ""), 10.5, fontname="china-s", indent=14)
        elif kind == "table":
            headers = block.get("headers", []) or []
            rows = block.get("rows", []) or []
            header_line = " | ".join(headers)
            cv.text(header_line, 10, fontname="china-s", indent=8)
            for row in rows:
                cells = [str(c) for c in row[: len(headers)]]
                cv.text("  | ".join(cells), 10, fontname="china-s", indent=12)
            cv.space(4)
    return cv.text_pdf()


# ---------- PPTX ----------


def _slides_to_pptx(slides: list[dict[str, Any]]) -> bytes:
    from io import BytesIO

    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    for slide in slides:
        title, body, takeaway = _slide_fields(slide)
        s = prs.slides.add_slide(title_layout)
        base = s.placeholders
        if title and base[0] is not None:
            base[0].text = title
        if body and len(base) > 1 and base[1] is not None:
            tf = base[1].text_frame
            tf.word_wrap = True
            tf.text = body
        if takeaway:
            tb = s.shapes.add_textbox(Inches(0.6), Inches(6.4), Inches(8.8), Inches(1.0))
            tf = tb.text_frame
            tf.text = takeaway
            tf.paragraphs[0].font.size = Pt(16)
            tf.paragraphs[0].font.bold = True
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _slides_to_pdf(slides: list[dict[str, Any]]) -> bytes:
    cv = _PdfCanvas()
    first = True
    for slide in slides:
        if not first:
            cv.new_page()
        first = False
        title, body, takeaway = _slide_fields(slide)
        cv.text(title, 26, fontname="china-s", block_gap=14)
        if body:
            cv.text(body, 14, fontname="china-s", block_gap=16)
        if takeaway:
            cv.text("▍ " + takeaway, 12, fontname="china-s", indent=16, block_gap=10)
    return cv.text_pdf()
