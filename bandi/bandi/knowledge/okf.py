# 功能：OKF 概念框架（Bandi Knowledge Format）——StaffDeck backend/app/knowledge/okf.py
# 概念检索评分（标题命中 > 正文命中，阈值 MIN_CONCEPT_SEARCH_SCORE）/ 概念卡 / OKF 引用。

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.knowledge.schema import KnowledgeBucket, KnowledgeConcept, KnowledgeDocument

MIN_CONCEPT_SEARCH_SCORE = 4.0
_CITATIONS_HEADING = "# Citations"
_CARD_CONTENT_CHARS = 6000


@dataclass
class ParsedOkfDocument:
    concept_id: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    content_md: str = ""


def safe_path_segment(value: Any, fallback: str = "concept") -> str:
    """功能：路径段安全化——保留中文（一等公民），剥离危险字符，空值回退。"""
    text = str(value or "").strip().lower()
    text = re.sub(r"\.md$", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:80] or fallback


def normalize_concept_id(value: str) -> str:
    """功能：concept_id 归一化——反斜杠转斜杠、逐段 safe_path_segment
    （路径穿越 .. / 绝对路径 / .md 后缀全部消除）。"""
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if text.endswith(".md"):
        text = text[:-3]
    parts = [safe_path_segment(part) for part in text.split("/") if part.strip()]
    return "/".join(part for part in parts if part)


def render_okf_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """功能：frontmatter + 正文渲染为 OKF markdown。"""
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines).strip() + "\n"


def parse_okf_markdown(concept_id: str, content_md: str) -> ParsedOkfDocument:
    """功能：解析 OKF markdown → ParsedOkfDocument（frontmatter/body/归一化全文）；
    缺省 type/title 回填。"""
    text = (content_md or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            raw = text[4:end].strip()
            body = text[end + 4 :].lstrip("\n")
            frontmatter = _parse_frontmatter(raw)
    if not frontmatter.get("type"):
        frontmatter["type"] = "Topic"
    if not frontmatter.get("title"):
        frontmatter["title"] = Path(concept_id).name.replace("-", " ").strip().title()
    normalized = render_okf_markdown(frontmatter, body)
    return ParsedOkfDocument(
        concept_id=normalize_concept_id(concept_id),
        frontmatter=frontmatter,
        body=body,
        content_md=normalized,
    )


def extract_links(markdown: str) -> list[dict[str, Any]]:
    """功能：提取 markdown 链接 [label](target)。"""
    links: list[dict[str, Any]] = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", markdown or ""):
        target = match.group(2).strip()
        if target:
            links.append({"label": match.group(1).strip(), "target": target})
    return links


def extract_citations(markdown: str) -> list[dict[str, Any]]:
    """功能：提取 # Citations 节内的 [n] [label](target) 引用（带序号缺省递增）。"""
    citations: list[dict[str, Any]] = []
    section = _citations_section(markdown or "")
    for index, match in enumerate(
        re.finditer(r"(?:\[(\d+)\]\s*)?\[([^\]]+)\]\(([^)]+)\)", section), start=1
    ):
        citations.append(
            {
                "index": int(match.group(1) or index),
                "label": match.group(2).strip(),
                "target": match.group(3).strip(),
            }
        )
    return citations


def build_okf_for_document(
    document: KnowledgeDocument,
    buckets: list[KnowledgeBucket],
) -> list[dict[str, Any]]:
    """功能：文档 → 概念集——1 个 Source Document 入口概念 + 每桶 1 个 Topic
    概念（规则锚定桶结构，ADR-33 无 LLM 提取）。"""
    doc_slug = safe_path_segment(document.title or Path(document.filename).stem or document.id)
    doc_slug = f"{doc_slug}-{_id_short(document.id)}"
    entry_concept_id = f"sources/{doc_slug}"
    concepts: list[dict[str, Any]] = [_entry_concept(document, entry_concept_id, buckets)]
    for bucket in buckets:
        concepts.append(_bucket_concept(document, entry_concept_id, bucket))
    return concepts


def upsert_candidates(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """功能：upsert 前清洗——concept_id/content_md 归一化，无效项丢弃。"""
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
    """功能：概念卡（检索命中呈现形态）——内容剥 frontmatter 并截断。"""
    return [
        {
            "id": row.id,
            "concept_id": row.concept_id,
            "type": row.concept_type,
            "title": row.title,
            "description": row.description,
            "links": row.links,
            "citations": row.citations,
            "source_refs": row.source_refs,
            "content": _strip_frontmatter(row.content_md).strip()[:_CARD_CONTENT_CHARS],
        }
        for row in concepts
    ]


def search_concepts(
    query: str, concepts: list[KnowledgeConcept], limit: int = 6
) -> list[KnowledgeConcept]:
    """功能：概念检索评分——标题区（id/type/title/description）命中 6.0+len
    权重高于正文命中 3.0+len*0.5；整句全命中额外 +10；Source Document 入口
    概念 -2 降权（入口页不当答案）；低于阈值不返回；空 query 返回空。
    中文 token 语义：连续 串整段为 token（substring 匹配）。"""
    query = (query or "").strip()
    if not query:
        return []
    tokens = _query_tokens(query)
    lower_query = query.lower()
    scored: list[tuple[float, float, KnowledgeConcept]] = []
    for row in concepts:
        heading_text = " ".join(
            [row.concept_id, row.concept_type, row.title, row.description or ""]
        ).lower()
        # （概念卡正文上限 6000 字符，量级可控）
        body_text = _strip_frontmatter(row.content_md).lower()
        haystack = f"{heading_text} {body_text}"
        score = 0.0
        matched = False
        for token in tokens:
            if not token:
                continue
            if token in heading_text:
                score += 6.0 + min(len(token), 6)
                matched = True
            elif token in body_text:
                score += 3.0 + min(len(token), 4) * 0.5
                matched = True
        if lower_query in haystack:
            score += 10
            matched = True
        if row.concept_type == "Source Document" and matched:
            score -= 2
        if matched and score >= MIN_CONCEPT_SEARCH_SCORE:
            scored.append((score, row.updated_at, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _score, _updated, row in scored[:limit]]


# 预留：从概念引用节构建 OKF 引用列表；启用时机 知识前端概念页
def okf_citations_for_concepts(concepts: list[KnowledgeConcept]) -> list[dict[str, Any]]:
    """功能：从概念 markdown 引用节构建 OKF 引用列表，(concept_id, target) 去重。"""
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


# -- 内部 -------------------------------------------------------------------


def _id_short(document_id: str) -> str:
    """功能：文档 id 短标识（锚定唯一性，同 title 文档 concept_id 不撞车）。"""
    return safe_path_segment(document_id)[-6:] or "doc"


def _entry_concept(
    document: KnowledgeDocument,
    concept_id: str,
    buckets: list[KnowledgeBucket],
) -> dict[str, Any]:
    title = document.title or Path(document.filename).stem or document.filename
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
            "# Citations",
            "",
            f"[1] [Original document]({resource})",
        ]
    )
    frontmatter = {
        "type": "Source Document",
        "title": title,
        "description": f"从 {document.filename} 生成的原始资料入口页。",
        "resource": resource,
        "tags": ["source", "business-knowledge"],
    }
    return {
        "concept_id": concept_id,
        "concept_type": "Source Document",
        "title": title,
        "description": frontmatter["description"],
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [{"document_id": document.id, "filename": document.filename}],
    }


def _bucket_concept(
    document: KnowledgeDocument,
    entry_concept_id: str,
    bucket: KnowledgeBucket,
) -> dict[str, Any]:
    concept_id = (
        f"{entry_concept_id}/buckets/"
        f"{safe_path_segment(bucket.bucket_key or bucket.title)}-{_id_short(bucket.id)}"
    )
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


def _query_tokens(query: str) -> list[str]:
    """功能：查询 token——连续 串整段 + 字母数字词（小写）。"""
    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query or ""):
        token = run.strip().lower()
        if token:
            tokens.append(token)
    return tokens


def _citations_section(markdown: str) -> str:
    """功能：取 # Citations 节正文（无该节返回空——不误收正文链接）。"""
    index = markdown.find(_CITATIONS_HEADING)
    if index < 0:
        return ""
    after = markdown[index + len(_CITATIONS_HEADING) :]
    next_heading = re.search(r"\n#[^#]", after)
    return after[: next_heading.start()] if next_heading else after


def _strip_frontmatter(markdown: str) -> str:
    """功能：剥离 frontmatter 只留正文。"""
    text = (markdown or "").strip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            return text[end + 4 :].lstrip("\n")
    return text


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """功能：极简 YAML 子集解析（key: scalar / 内联 list），不引三方依赖。"""
    frontmatter: dict[str, Any] = {}
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        frontmatter[key] = _parse_scalar(value)
    return frontmatter


def _parse_scalar(value: str) -> Any:
    """功能：标量解析——内联 list / 去引号字符串。"""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _yaml_scalar(value: Any) -> str:
    """功能：标量序列化——list → 内联形式，字符串原样。"""
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)
