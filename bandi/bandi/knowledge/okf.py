# 功能：知识概念层（OKF）——概念构建、markdown 往返、概念检索评分与概念卡输出。

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.knowledge.schema import KnowledgeBucket, KnowledgeConcept, KnowledgeDocument

# 概念检索的入选门槛：低于该分数的概念不作为答案返回。
MIN_CONCEPT_SEARCH_SCORE = 4.0

_CITATIONS_TITLE = "# Citations"
_CARD_CONTENT_CHARS = 6000

# 评分权重：标题区命中远高于正文命中，整句命中额外加成。
_HEADING_BASE = 6.0
_HEADING_TOKEN_CAP = 6
_BODY_BASE = 3.0
_BODY_TOKEN_FACTOR = 0.5
_BODY_TOKEN_CAP = 4
_WHOLE_QUERY_BONUS = 10.0
_SOURCE_ENTRY_PENALTY = 2.0

_SAFE_SEGMENT = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_MD_SUFFIX = re.compile(r"\.md$")
_TOKEN_RUN = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CITATION_LINK = re.compile(r"(?:\[(\d+)\]\s*)?\[([^\]]+)\]\(([^)]+)\)")
_NEXT_HEADING = re.compile(r"\n#[^#]")


@dataclass
class ParsedOkfDocument:
    concept_id: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    content_md: str = ""


def safe_path_segment(value: Any, fallback: str = "concept") -> str:
    """功能：把任意文本转成可用作路径段的片段（中文保留，其余非法字符折叠为 -）。"""
    text = str(value or "").strip().lower()
    text = _MD_SUFFIX.sub("", text)
    text = _SAFE_SEGMENT.sub("-", text).strip("-")
    return text[:80] or fallback


def normalize_concept_id(value: str) -> str:
    """功能：归一化概念 id——反斜杠转斜杠、逐段安全化，消除路径穿越与 .md 后缀。"""
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if text.endswith(".md"):
        text = text[:-3]
    segments = [safe_path_segment(part) for part in text.split("/") if part.strip()]
    return "/".join(segment for segment in segments if segment)


def _render_scalar(value: Any) -> str:
    """功能：把 frontmatter 值序列化成一行标量（列表用内联形式）。"""
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def render_okf_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """功能：把 frontmatter 与正文拼成 OKF markdown 文本。"""
    header = ["---"]
    header.extend(f"{key}: {_render_scalar(value)}" for key, value in frontmatter.items())
    header.append("---")
    block = "\n".join([*header, "", body.strip()])
    return block.strip() + "\n"


def _read_scalar(value: str) -> Any:
    """功能：解析单个标量——内联列表或去引号字符串。"""
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _read_frontmatter(raw: str) -> dict[str, Any]:
    """功能：解析 YAML 的 key: scalar 子集，不引入外部依赖。"""
    parsed: dict[str, Any] = {}
    for line in (raw or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            continue
        parsed[key.strip()] = _read_scalar(value)
    return parsed


def _drop_frontmatter(markdown: str) -> str:
    """功能：去掉 frontmatter，只留正文。"""
    text = (markdown or "").strip()
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :].lstrip("\n") if end >= 0 else text


def _fallback_title(concept_id: str) -> str:
    return Path(concept_id).name.replace("-", " ").strip().title()


def parse_okf_markdown(concept_id: str, content_md: str) -> ParsedOkfDocument:
    """功能：解析 OKF markdown；缺失的 type/title 用默认值补齐并重新渲染。"""
    text = (content_md or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            frontmatter = _read_frontmatter(text[4:end].strip())
            body = text[end + 4 :].lstrip("\n")
    frontmatter.setdefault("type", "Topic")
    if not frontmatter.get("title"):
        frontmatter["title"] = _fallback_title(concept_id)
    return ParsedOkfDocument(
        concept_id=normalize_concept_id(concept_id),
        frontmatter=frontmatter,
        body=body,
        content_md=render_okf_markdown(frontmatter, body),
    )


def extract_links(markdown: str) -> list[dict[str, Any]]:
    """功能：提取正文中的 [label](target) 链接。"""
    links: list[dict[str, Any]] = []
    for match in _LINK.finditer(markdown or ""):
        target = match.group(2).strip()
        if target:
            links.append({"label": match.group(1).strip(), "target": target})
    return links


def _citations_block(markdown: str) -> str:
    """功能：截取 # Citations 小节正文，没有该小节时返回空。"""
    start = markdown.find(_CITATIONS_TITLE)
    if start < 0:
        return ""
    rest = markdown[start + len(_CITATIONS_TITLE) :]
    stop = _NEXT_HEADING.search(rest)
    return rest[: stop.start()] if stop else rest


def extract_citations(markdown: str) -> list[dict[str, Any]]:
    """功能：提取 # Citations 小节内的引用条目（序号可省略，按出现次序递增）。"""
    citations: list[dict[str, Any]] = []
    for position, match in enumerate(_CITATION_LINK.finditer(_citations_block(markdown or "")), 1):
        citations.append(
            {
                "index": int(match.group(1) or position),
                "label": match.group(2).strip(),
                "target": match.group(3).strip(),
            }
        )
    return citations


def _short_id(raw_id: str) -> str:
    """功能：取 id 尾部片段，保证同名文档的概念 id 不会互相覆盖。"""
    return safe_path_segment(raw_id)[-6:] or "doc"


def _document_title(document: KnowledgeDocument) -> str:
    return document.title or Path(document.filename).stem or document.filename


def _entry_concept(
    document: KnowledgeDocument,
    concept_id: str,
    buckets: list[KnowledgeBucket],
) -> dict[str, Any]:
    """功能：文档入口概念（Source Document），列出最多 30 个知识桶链接。"""
    title = _document_title(document)
    resource = f"ultrarag://knowledge/documents/{document.id}"
    bucket_lines = [
        f"- [{bucket.title}](/topics/{safe_path_segment(bucket.bucket_key or bucket.title)}.md)"
        f" - {bucket.summary}"
        for bucket in buckets[:30]
    ]
    body = "\n".join(
        [
            "# Summary",
            "",
            f"从 {document.filename} 生成的 OKF 文档入口。",
            "",
            "# Knowledge Buckets",
            "",
            *(bucket_lines or ["- 暂无知识桶"]),
            "",
            _CITATIONS_TITLE,
            "",
            f"[1] [Original document]({resource})",
        ]
    )
    description = f"从 {document.filename} 生成的原始资料入口页。"
    frontmatter = {
        "type": "Source Document",
        "title": title,
        "description": description,
        "resource": resource,
        "tags": ["source", "business-knowledge"],
    }
    return {
        "concept_id": concept_id,
        "concept_type": "Source Document",
        "title": title,
        "description": description,
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [{"document_id": document.id, "filename": document.filename}],
    }


def _topic_concept(
    document: KnowledgeDocument,
    entry_concept_id: str,
    bucket: KnowledgeBucket,
) -> dict[str, Any]:
    """功能：知识桶概念（Topic），摘要缺省时回落到桶标题。"""
    segment = safe_path_segment(bucket.bucket_key or bucket.title)
    concept_id = f"{entry_concept_id}/buckets/{segment}-{_short_id(bucket.id)}"
    summary = bucket.summary or bucket.title
    body = "\n".join(["# Summary", "", summary or "暂无摘要。"])
    frontmatter = {
        "type": "Topic",
        "title": bucket.title,
        "description": summary,
        "resource": f"ultrarag://knowledge/buckets/{bucket.id}",
        "tags": ["bucket"],
    }
    return {
        "concept_id": concept_id,
        "concept_type": "Topic",
        "title": bucket.title,
        "description": summary,
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [
            {"document_id": document.id, "bucket_id": bucket.id, "filename": document.filename}
        ],
    }


def build_okf_for_document(
    document: KnowledgeDocument,
    buckets: list[KnowledgeBucket],
) -> list[dict[str, Any]]:
    """功能：文档 → 概念集（1 个入口概念 + 每个知识桶 1 个主题概念）。"""
    slug = safe_path_segment(document.title or Path(document.filename).stem or document.id)
    entry_concept_id = f"sources/{slug}-{_short_id(document.id)}"
    concepts: list[dict[str, Any]] = [_entry_concept(document, entry_concept_id, buckets)]
    concepts.extend(_topic_concept(document, entry_concept_id, bucket) for bucket in buckets)
    return concepts


def upsert_candidates(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """功能：写库前清洗——归一化 concept_id，丢掉缺 id 或缺正文的条目。"""
    cleaned: list[dict[str, Any]] = []
    for item in concepts:
        if not isinstance(item, dict):
            continue
        concept_id = normalize_concept_id(str(item.get("concept_id") or ""))
        content_md = str(item.get("content_md") or "")
        if not concept_id or not content_md:
            continue
        cleaned.append({**item, "concept_id": concept_id})
    return cleaned


def selected_concept_cards(concepts: list[KnowledgeConcept]) -> list[dict[str, Any]]:
    """功能：把命中概念整理成前端卡片（正文去 frontmatter 并截断）。"""
    cards: list[dict[str, Any]] = []
    for row in concepts:
        cards.append(
            {
                "id": row.id,
                "concept_id": row.concept_id,
                "type": row.concept_type,
                "title": row.title,
                "description": row.description,
                "links": row.links,
                "citations": row.citations,
                "source_refs": row.source_refs,
                "content": _drop_frontmatter(row.content_md).strip()[:_CARD_CONTENT_CHARS],
            }
        )
    return cards


def _query_tokens(query: str) -> list[str]:
    """功能：切查询词——中文连续串整体成词，字母数字串成词，统一小写。"""
    return [run.strip().lower() for run in _TOKEN_RUN.findall(query or "") if run.strip()]


def _score_concept(
    row: KnowledgeConcept,
    tokens: Iterable[str],
    lower_query: str,
) -> tuple[float, bool]:
    """功能：概念相关度——标题区权重高于正文，命中整句额外加成。"""
    heading_text = " ".join(
        [row.concept_id, row.concept_type, row.title, row.description or ""]
    ).lower()
    body_text = _drop_frontmatter(row.content_md).lower()

    score = 0.0
    matched = False
    for token in tokens:
        if token in heading_text:
            score += _HEADING_BASE + min(len(token), _HEADING_TOKEN_CAP)
            matched = True
        elif token in body_text:
            score += _BODY_BASE + min(len(token), _BODY_TOKEN_CAP) * _BODY_TOKEN_FACTOR
            matched = True
    if lower_query and lower_query in f"{heading_text} {body_text}":
        score += _WHOLE_QUERY_BONUS
        matched = True
    if matched and row.concept_type == "Source Document":
        score -= _SOURCE_ENTRY_PENALTY
    return score, matched


def search_concepts(
    query: str,
    concepts: list[KnowledgeConcept],
    limit: int = 6,
) -> list[KnowledgeConcept]:
    """功能：概念检索——按相关度降序返回，低于门槛或空查询时不返回。"""
    text = (query or "").strip()
    if not text:
        return []

    tokens = _query_tokens(text)
    lower_query = text.lower()
    ranked: list[tuple[float, float, KnowledgeConcept]] = []
    for row in concepts:
        score, matched = _score_concept(row, tokens, lower_query)
        if matched and score >= MIN_CONCEPT_SEARCH_SCORE:
            ranked.append((score, row.updated_at, row))
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [row for _score, _updated_at, row in ranked[:limit]]


# 预留：知识前端概念页启用时消费——当前只做去重聚合，不参与注入。
def okf_citations_for_concepts(concepts: list[KnowledgeConcept]) -> list[dict[str, Any]]:
    """功能：汇总概念里的引用，(concept_id, target) 去重后返回。"""
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in concepts:
        for citation in row.citations or []:
            target = str(citation.get("target") or "")
            key = (row.concept_id, target)
            if not target or key in seen:
                continue
            seen.add(key)
            citations.append(
                {
                    "concept_id": row.concept_id,
                    "title": row.title,
                    "label": citation.get("label") or citation.get("text") or target,
                    "target": target,
                }
            )
    return citations
