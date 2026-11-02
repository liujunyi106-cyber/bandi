# 功能：把检索命中整理成引用列表，并按正文引用顺序重编号；同时清掉模型自拟的尾部来源清单。

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# 单条引用正文/摘要的最大字符数（超出即截断，避免整段文档灌进上下文）。
CITATION_EXCERPT_CHAR_LIMIT = 6000
CITATION_SUMMARY_CHAR_LIMIT = 800

# 引用块的识别边界：最多 40 行、6000 字符，超过就不当作"模型自拟清单"处理。
_MAX_BLOCK_LINES = 40
_MAX_BLOCK_CHARS = 6000

_LABEL_PATTERN = re.compile(r"\[(\d+)\]")
_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_FILE_PATTERN = re.compile(r"\.(?:md|markdown|pdf|docx?|xlsx?|csv|txt|json)\b", re.IGNORECASE)
_LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.、])\s+")
_HEADING_PREFIX = r"^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?\s*"
_HEADING_SUFFIX = r"\s*(?:\*\*|__)?\s*(?:[:：].*)?$"

# "来源"这类泛称需要配合列表形态才判定；"引用来源"等明确标题可单独成立。
_LOOSE_SOURCE_HEADING = re.compile(
    _HEADING_PREFIX + r"(?:引用来源|参考来源|资料来源|信息来源|来源)" + _HEADING_SUFFIX
)
_STRICT_SOURCE_HEADING = re.compile(
    _HEADING_PREFIX + r"(?:引用来源|参考来源|资料来源|信息来源)" + _HEADING_SUFFIX
)

# 命中任一强特征即认为该块确实在列引用，而不是正文里的普通"来源：……"表述。
_STRONG_HINTS = ("chunk_id", "document_id", "bucket_id", "知识库条目", "source_path")


def _squeeze(value: str) -> str:
    """功能：折叠内部空白，得到单行文本。"""
    return " ".join((value or "").split())


def _truncate(value: str, limit: int) -> str:
    """功能：超长文本截断并补省略号，长度不超过 limit。"""
    text = _squeeze(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _dedupe_key(value: str) -> str:
    """功能：生成去重键——小写并去掉所有非字母数字字符。"""
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _title_of(value: str) -> str:
    return _truncate(value, 72)


class _CitationLedger:
    """功能：按去重键累积引用，编号与 id 随条目数递增。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._keys: set[str] = set()
        self.items: list[dict[str, Any]] = []

    def full(self) -> bool:
        return len(self.items) >= self._limit

    def push(self, kind: str, key_source: str, title: str, payload: dict[str, Any]) -> None:
        """功能：键或标题任一已出现过则跳过，否则作为第 n 条引用追加。"""
        if self.full():
            return
        key = _dedupe_key(key_source)
        title_key = _dedupe_key(title)
        if not key or key in self._keys or (title_key and title_key in self._keys):
            return
        self._keys.update({key, title_key} - {""})
        number = len(self.items) + 1
        self.items.append(
            {
                "id": f"kref_{number}",
                "label": f"[{number}]",
                "kind": kind,
                **payload,
            }
        )


def _concept_payload(concept: dict[str, Any], concept_id: str, title: str) -> dict[str, Any]:
    body = str(concept.get("content") or concept.get("content_excerpt") or "").strip()
    return {
        "title": title,
        "content": body[:CITATION_EXCERPT_CHAR_LIMIT],
        "excerpt": body[:CITATION_EXCERPT_CHAR_LIMIT],
        "summary": str(concept.get("description") or "")[:CITATION_SUMMARY_CHAR_LIMIT],
        "concept_id": concept_id,
    }


def _evidence_identity(item: dict[str, Any], excerpt: str, summary: str, source_path: str) -> str:
    """功能：摘要 > 路径+正文前缀 > chunk_id，依次取第一个可用的身份来源。"""
    if summary:
        return summary
    if source_path or excerpt:
        return f"{source_path}:{excerpt[:120]}"
    return str(item.get("chunk_id") or "").strip()


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    """功能：从检索结果构建引用列表——先取概念，再取证据片段，按出现顺序编号。"""
    ledger = _CitationLedger(limit)

    for result in knowledge_results:
        if not isinstance(result, dict):
            continue

        for concept in result.get("selected_concepts") or []:
            if not isinstance(concept, dict) or ledger.full():
                continue
            concept_id = str(concept.get("concept_id") or concept.get("id") or "").strip()
            title = _title_of(str(concept.get("title") or concept_id or "知识概念"))
            ledger.push(
                "concept",
                concept_id or title,
                title,
                _concept_payload(concept, concept_id, title),
            )

        for item in result.get("evidence_pack") or []:
            if not isinstance(item, dict) or ledger.full():
                continue
            excerpt = str(item.get("content") or item.get("excerpt") or "").strip()
            summary = str(item.get("summary") or "").strip()
            source_path = str(item.get("source_path") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = _title_of(
                str(item.get("title") or "").strip() or source_path or summary or "知识片段"
            )
            ledger.push(
                "evidence",
                _evidence_identity(item, excerpt, summary, source_path),
                title,
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

    return ledger.items


def _labels_in_use(content: str) -> list[int]:
    """功能：按首次出现顺序收集正文里出现过的引用编号。"""
    ordered: list[int] = []
    for match in _LABEL_PATTERN.finditer(content):
        number = int(match.group(1))
        if number not in ordered:
            ordered.append(number)
    return ordered


def compact_citation_labels(
    content: str,
    citations: object,
) -> tuple[str, list[dict[str, Any]]]:
    """功能：按正文首次出现顺序重编号引用标注，正文未引用的条目不保留。"""
    if not isinstance(citations, list) or not citations:
        return content, []

    by_number: dict[int, dict[str, Any]] = {}
    for position, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        match = _LABEL_PATTERN.fullmatch(str(citation.get("label") or "").strip())
        number = int(match.group(1)) if match else position
        by_number.setdefault(number, citation)

    used = [number for number in _labels_in_use(content) if number in by_number]
    if not used:
        return content, []

    remap = {old: new for new, old in enumerate(used, start=1)}

    def _sub(match: re.Match[str]) -> str:
        target = remap.get(int(match.group(1)))
        return f"[{target}]" if target is not None else match.group(0)

    rebuilt = _LABEL_PATTERN.sub(_sub, content)
    reordered = [{**by_number[old], "label": f"[{remap[old]}]"} for old in used]
    return rebuilt, reordered


def _has_citation_evidence(block: str) -> bool:
    """功能：块内出现内部标识/[n]/URL/文件扩展名任一，即视为引用清单。"""
    lowered = block.lower()
    if any(hint in lowered for hint in _STRONG_HINTS):
        return True
    return bool(
        _LABEL_PATTERN.search(block) or _URL_PATTERN.search(block) or _FILE_PATTERN.search(block)
    )


def _last_heading_index(lines: list[str]) -> int:
    """功能：从尾部往前找最后一个来源标题行，找不到返回 -1。"""
    for index in range(len(lines) - 1, -1, -1):
        if _LOOSE_SOURCE_HEADING.match(lines[index]):
            return index
    return -1


def _looks_like_list_block(lines: Iterable[str]) -> bool:
    return sum(1 for line in lines if _LIST_LINE.match(line)) >= 2


def strip_generated_citation_block(content: str) -> str:
    """功能：删除回复末尾由模型自拟的来源清单。

    仅处理尾部块，且块内必须带引用特征（[n]/URL/文件扩展名/内部标识），
    避免误删正文中正常的"来源"表述；整条回复只有清单时保持原样。
    """
    text = content or ""
    if not text.strip():
        return content

    lines = text.splitlines()
    heading = _last_heading_index(lines)
    if heading < 0:
        return content
    if len(lines) - heading > _MAX_BLOCK_LINES:
        return content

    block = "\n".join(lines[heading:])
    if len(block) > _MAX_BLOCK_CHARS or not _has_citation_evidence(block):
        return content

    if not _STRICT_SOURCE_HEADING.match(lines[heading]) and (
        not _looks_like_list_block(lines[heading + 1 :])
        and len(_LABEL_PATTERN.findall(block)) < 2
    ):
        return content

    head = lines[:heading]
    if "\n".join(head).count("```") % 2 == 1:
        return content

    kept = "\n".join(head).rstrip()
    return kept if kept.strip() else content
