# 功能：pdf_research_core — 纯 PyMuPDF 学术 PDF 分析核心（结构/引用/文本/区域提取）

from __future__ import annotations

import re
from typing import Any

from bandi.tools.doc._doc_utils import import_fitz as _import_fitz

_SECTION_KEYWORDS = [
    "abstract",
    "introduction",
    "background",
    "related work",
    "method",
    "methodology",
    "approach",
    "proposed",
    "experiment",
    "results",
    "evaluation",
    "discussion",
    "conclusion",
    "future work",
    "references",
    "appendix",
    "limitation",
    "analysis",
    "implementation",
    "dataset",
    "architecture",
    "overview",
    "model",
    "algorithm",
    "applications",
    "summary",
]

_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,2})\.?\s+([A-Z][^\n]{2,80})$")
_FIGURE_RE = re.compile(
    r"\b(?:Fig(?:ure)?\.?\s*(\d+)|Table\s*(\d+)|图\s*(\d+)|表\s*(\d+))\b", re.IGNORECASE
)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_MAX_SECTIONS = 80
_MAX_FIGURES = 120
_MAX_CITATIONS = 200


def extract_structure(file_path: str) -> dict[str, Any]:
    """提取 PDF 结构：页数/标题/书签/章节/图表清单。"""
    fitz = _import_fitz()
    with fitz.open(file_path) as doc:
        page_count = len(doc)
        title = doc.metadata.get("title") or None
        outline = [
            {"title": t, "page": p, "level": lvl}
            for lvl, t, p in doc.get_toc(simple=True)
            if t.strip()
        ]
        sections = _derive_sections(doc, outline)
        figures = _derive_figures(doc)
    return {
        "page_count": page_count,
        "title": title,
        "outline": outline,
        "sections": sections,
        "figures": figures,
        # 保守标记（达到上限即可能截断），并暴露限额口径
        "sections_truncated": len(sections) >= _MAX_SECTIONS,
        "figures_truncated": len(figures) >= _MAX_FIGURES,
        "limits": {"sections": _MAX_SECTIONS, "figures": _MAX_FIGURES},
    }


def _derive_sections(doc, outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if outline:
        for i, item in enumerate(outline, start=1):
            if len(sections) >= _MAX_SECTIONS:
                break
            if item["level"] > 2:
                continue
            sections.append(
                {
                    "id": f"sec-{i}",
                    "title": item["title"],
                    "page": item["page"],
                    "level": item["level"],
                }
            )
        return sections
    seen: set[str] = set()
    for page_num in range(min(len(doc), 40)):
        page = doc[page_num]
        for line in page.get_text("text").splitlines():
            line = line.strip()
            if not line:
                continue
            m = _NUMBERED_HEADING_RE.match(line)
            if m:
                title = m.group(2).strip()
                if title.lower() in seen:
                    continue
                seen.add(title.lower())
                sections.append(
                    {
                        "id": f"sec-{len(sections) + 1}",
                        "title": title,
                        "page": page_num + 1,
                        "level": min(3, m.group(1).count(".") + 1),
                    }
                )
                if len(sections) >= _MAX_SECTIONS:
                    return sections
                continue
            lower = line.lower().rstrip(".:")
            if lower in _SECTION_KEYWORDS and lower not in seen:
                seen.add(lower)
                sections.append(
                    {
                        "id": f"sec-{len(sections) + 1}",
                        "title": line.rstrip(".:"),
                        "page": page_num + 1,
                        "level": 2,
                    }
                )
                if len(sections) >= _MAX_SECTIONS:
                    return sections
    return sections


def _derive_figures(doc) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_num in range(len(doc)):
        page = doc[page_num]
        for line in page.get_text("text").splitlines():
            for m in _FIGURE_RE.finditer(line):
                label = next((g for g in m.groups() if g), "")
                # （英文 Table）会把它标为 figure（语义错误）
                is_table = bool(m.group(2) or m.group(4))
                key = f"{'table' if is_table else 'figure'}:{label}"
                if key in seen:
                    continue
                seen.add(key)
                figures.append(
                    {
                        "id": f"fig-{len(figures) + 1}",
                        "label": label,
                        "page": page_num + 1,
                        "kind": "table" if is_table else "figure",
                    }
                )
                if len(figures) >= _MAX_FIGURES:
                    return figures
    return figures


def extract_citations(file_path: str, max_items: int = _MAX_CITATIONS) -> list[dict[str, Any]]:
    """提取参考文献列表（找 References/Bibliography 起点后切分条目）。"""
    fitz = _import_fitz()
    with fitz.open(file_path) as doc:
        all_text = "\n".join(page.get_text("text") for page in doc)
    entries = _split_reference_entries(all_text)
    citations: list[dict[str, Any]] = []
    for i, entry in enumerate(entries, start=1):
        if len(citations) >= max_items:
            break
        if not entry.strip():
            continue
        text = re.sub(r"\s+", " ", entry).strip()[:600]
        doi = _DOI_RE.search(text)
        url = _URL_RE.search(text)
        marker = ""
        m = re.match(r"^\[(\d{1,3})\]", text)
        if m:
            marker = f"[{m.group(1)}]"
        citations.append(
            {
                "id": f"cit-{i}",
                "marker": marker,
                "text": text,
                "doi": doi.group(0) if doi else None,
                "url": url.group(0) if url else None,
            }
        )
    return citations


def _split_reference_entries(text: str) -> list[str]:
    start = _find_references_start(text)
    if start is None:
        return []
    body = text[start:]
    numbered = re.split(r"\n\s*(?=\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.\s)", body)
    if len(numbered) >= 3:
        return [p for p in numbered if p.strip()]
    blocks = [p for p in body.split("\n\n") if len(p.strip()) > 20]
    if len(blocks) >= 3:
        return blocks
    return [line for line in body.splitlines() if line.strip()]


def _find_references_start(text: str) -> int | None:
    for pattern in (r"^\s*(References|Bibliography|参考文献|参考文献列表)\s*$",):
        m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.end()
    return None


def extract_page_text(
    file_path: str, start_page: int | None = None, end_page: int | None = None
) -> str:
    """提取页区间纯文本（1-based 闭区间）。"""
    fitz = _import_fitz()
    with fitz.open(file_path) as doc:
        total = len(doc)
        s = start_page or 1
        e = min(end_page or total, total)
        parts: list[str] = []
        for i in range(s - 1, e):
            parts.append(doc[i].get_text("text"))
    return "\n\n".join(p for p in parts if p.strip())


def extract_region_text(file_path: str, page: int, bbox: list[float] | tuple[float, ...]) -> str:
    """提取页面指定区域文本（bbox = [x0, y0, x1, y1]，PDF points）。"""
    fitz = _import_fitz()
    with fitz.open(file_path) as doc:
        if page < 1 or page > len(doc):
            return ""
        clip = fitz.Rect(*bbox)
        return doc[page - 1].get_text("text", clip=clip)
