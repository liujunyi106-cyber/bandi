# 功能：知识库引用链（citation）——参考实现 backend/app/knowledge/citations.py
# 从检索结果构建去重引用、按正文首次出现重编号；excerpt/summary 截断上限对齐。

from __future__ import annotations

import re
from typing import Any

CITATION_EXCERPT_CHAR_LIMIT = 6000
CITATION_SUMMARY_CHAR_LIMIT = 800

_CITATION_BLOCK_HEADER = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?\s*(?:引用来源|参考来源|资料来源|信息来源|来源)"
    r"\s*(?:\*\*|__)?\s*(?:[:：].*)?$"
)
_EXPLICIT_CITATION_HEADER = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?\s*(?:引用来源|参考来源|资料来源|信息来源)"
    r"\s*(?:\*\*|__)?\s*(?:[:：].*)?$"
)
_CITATION_LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.、])\s+")
_CITATION_BLOCK_MAX_LINES = 40
_CITATION_BLOCK_MAX_CHARS = 6000
_CITATION_STRONG_MARKERS = (
    "chunk_id",
    "document_id",
    "bucket_id",
    "知识库条目",
    "source_path",
)
_CITATION_LABEL = re.compile(r"\[\d+\]")
_CITATION_URL = re.compile(r"https?://", re.IGNORECASE)
_CITATION_FILE = re.compile(r"\.(?:md|markdown|pdf|docx?|xlsx?|csv|txt|json)\b", re.IGNORECASE)


def _compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _normalize_identity(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _semantic_identity(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _display_title(value: str) -> str:
    return _compact(value, 72)


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    """功能：从检索结果构建引用列表——concepts 优先、evidence_pack 次之；
    identity 去重、limit 截断、label 连续编号（[1]..[n]）。"""
    citations: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    def add(kind: str, identity: str, payload: dict[str, Any]) -> None:
        if len(citations) >= limit:
            return
        normalized = _normalize_identity(identity)
        title_identity = _normalize_identity(str(payload.get("title") or ""))
        if not normalized or normalized in seen_identities:
            return
        if title_identity and title_identity in seen_identities:
            return
        seen_identities.add(normalized)
        if title_identity:
            seen_identities.add(title_identity)
        citations.append(
            {
                "id": f"kref_{len(citations) + 1}",
                "label": f"[{len(citations) + 1}]",
                "kind": kind,
                **payload,
            }
        )

    for result in knowledge_results:
        if not isinstance(result, dict):
            continue
        for concept in result.get("selected_concepts") or []:
            if not isinstance(concept, dict):
                continue
            concept_id = str(concept.get("concept_id") or concept.get("id") or "").strip()
            title = _display_title(str(concept.get("title") or concept_id or "知识概念"))
            excerpt = str(concept.get("content") or concept.get("content_excerpt") or "").strip()
            add(
                "concept",
                concept_id or title,
                {
                    "title": title,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": str(concept.get("description") or "")[:CITATION_SUMMARY_CHAR_LIMIT],
                    "concept_id": concept_id,
                },
            )
        for item in result.get("evidence_pack") or []:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("content") or item.get("excerpt") or "").strip()
            summary = str(item.get("summary") or "").strip()
            source_path = str(item.get("source_path") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = _display_title(
                str(item.get("title") or "").strip() or source_path or summary or "知识片段"
            )
            if summary:
                identity_source = summary
            elif source_path or excerpt:
                identity_source = f"{source_path}:{excerpt[:120]}"
            else:
                identity_source = chunk_id
            identity = _semantic_identity(identity_source)
            add(
                "evidence",
                identity,
                {
                    "title": title,
                    "source_path": source_path,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": summary[:CITATION_SUMMARY_CHAR_LIMIT],
                    "document_id": item.get("document_id"),
                    "bucket_id": item.get("bucket_id"),
                    "chunk_id": chunk_id,
                },
            )
    return citations


def compact_citation_labels(
    content: str,
    citations: object,
) -> tuple[str, list[dict[str, Any]]]:
    """功能：按正文引用标注 [n] 首次出现顺序重编号；未在正文出现的引用丢弃。
    返回（改写后正文，重编号引用列表）。"""
    if not isinstance(citations, list) or not citations:
        return content, []

    citations_by_label: dict[int, dict[str, Any]] = {}
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        label_match = re.fullmatch(r"\[(\d+)\]", str(citation.get("label") or "").strip())
        label = int(label_match.group(1)) if label_match else index
        citations_by_label.setdefault(label, citation)

    ordered_labels: list[int] = []
    for match in re.finditer(r"\[(\d+)\]", content):
        label = int(match.group(1))
        if label in citations_by_label and label not in ordered_labels:
            ordered_labels.append(label)

    if not ordered_labels:
        return content, []

    label_mapping = {old: new for new, old in enumerate(ordered_labels, start=1)}

    def replace_label(match: re.Match[str]) -> str:
        new_label = label_mapping.get(int(match.group(1)))
        return f"[{new_label}]" if new_label is not None else match.group(0)

    compacted_content = re.sub(r"\[(\d+)\]", replace_label, content)
    compacted_citations = [
        {**citations_by_label[old], "label": f"[{label_mapping[old]}]"} for old in ordered_labels
    ]
    return compacted_content, compacted_citations


def strip_generated_citation_block(content: str) -> str:
    """功能：删除模型在回复末尾自写的来源清单（引用来源/参考来源等）。

    只处理**尾部**块且块内必须有引用特征（[n]/URL/文件扩展名/内部标识），
    避免误删正文中正常的「来源」表述；整条回复只有来源清单时保留原文。
    """
    text = content or ""
    if not text.strip():
        return content
    lines = text.splitlines()
    header_index = -1
    for index in range(len(lines) - 1, -1, -1):
        if _CITATION_BLOCK_HEADER.match(lines[index]):
            header_index = index
            break
    if header_index < 0:
        return content
    if len(lines) - header_index > _CITATION_BLOCK_MAX_LINES:
        return content
    block = "\n".join(lines[header_index:])
    if len(block) > _CITATION_BLOCK_MAX_CHARS:
        return content
    if not _looks_like_citation_list(block):
        return content
    if not _EXPLICIT_CITATION_HEADER.match(lines[header_index]):
        list_lines = [line for line in lines[header_index + 1 :] if _CITATION_LIST_LINE.match(line)]
        if len(list_lines) < 2 and len(_CITATION_LABEL.findall(block)) < 2:
            return content
    before = "\n".join(lines[:header_index])
    if before.count("```") % 2 == 1:
        return content
    kept = before.rstrip()
    if not kept.strip():
        return content
    return kept


def _looks_like_citation_list(block: str) -> bool:
    """功能：判断块是否含引用特征，防止把普通结尾的「来源：」散文误删。"""
    lowered = block.lower()
    if any(marker in lowered for marker in _CITATION_STRONG_MARKERS):
        return True
    if _CITATION_LABEL.search(block) or _CITATION_URL.search(block):
        return True
    return bool(_CITATION_FILE.search(block))
