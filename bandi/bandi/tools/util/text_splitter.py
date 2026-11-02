# 功能：text_splitter — 文本分块（字符/token/句/段/行/regex/smart，7 个 op；基准 LeAgent tools/util/text_splitter.py 移植）
from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


def _chunk_dict(text: str, index: int, start: int, end: int) -> dict[str, Any]:
    return {"text": text, "index": index, "start": start, "end": end, "length": len(text)}


class TextSplitterTool(BaseTool):
    """功能：按字符/token/句/段落/行/正则/smart 七种策略切分文本。"""

    name = "text_splitter"
    description = "文本分块：split_chars/split_tokens/split_sentences/split_paragraphs/split_lines/split_regex/smart_split"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "split_chars",
                    "split_tokens",
                    "split_sentences",
                    "split_paragraphs",
                    "split_lines",
                    "split_regex",
                    "smart_split",
                ],
                "description": "切分策略",
            },
            "text": {"type": "string", "description": "待切分文本"},
            "chunk_size": {"type": "integer", "description": "分块大小"},
            "chunk_overlap": {"type": "integer", "description": "块间重叠"},
            "pattern": {"type": "string", "description": "split_regex 分隔正则"},
            "separators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "smart_split 分隔候选",
            },
            "preserve_words": {"type": "boolean", "default": True},
            "strip_chunks": {"type": "boolean", "default": True},
            "min_chunk_size": {"type": "integer", "default": 0},
            "max_chunks": {"type": "integer"},
            "include_metadata": {"type": "boolean", "default": True},
            "keep_separator": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "split_chars")
        text = params.get("text", "")
        if not text:
            return {"chunks": [], "count": 0, "total_length": 0}
        operations = {
            "split_chars": self._split_by_chars,
            "split_tokens": self._split_by_tokens,
            "split_sentences": self._split_by_sentences,
            "split_paragraphs": self._split_by_paragraphs,
            "split_lines": self._split_by_lines,
            "split_regex": self._split_by_regex,
            "smart_split": self._smart_split,
        }
        if operation not in operations:
            raise ValueError(f"Unknown operation: {operation}")
        chunks = operations[operation](text, params)
        max_chunks = params.get("max_chunks")
        if max_chunks and len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]
        include_metadata = params.get("include_metadata", True)
        if not include_metadata:
            chunks = [c["text"] if isinstance(c, dict) else c for c in chunks]
        result: dict[str, Any] = {
            "chunks": chunks,
            "count": len(chunks),
            "total_length": len(text),
        }
        if include_metadata and chunks:
            sizes = [len(c["text"]) if isinstance(c, dict) else len(c) for c in chunks]
            result["statistics"] = {
                "avg_chunk_size": sum(sizes) // len(sizes),
                "min_chunk_size": min(sizes),
                "max_chunk_size": max(sizes),
                "chunk_count": len(chunks),
            }
        return result

    def _split_by_chars(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 1000)
        overlap = params.get("chunk_overlap", 0)
        preserve_words = params.get("preserve_words", True)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        if overlap >= chunk_size:
            raise ValueError("Overlap must be less than chunk size")
        chunks: list[dict[str, Any]] = []
        start = 0
        index = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if preserve_words and end < len(text):
                word_boundary = text.rfind(" ", start, end)
                if word_boundary > start:
                    end = word_boundary
            chunk_text = text[start:end]
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if len(chunk_text) >= min_chunk_size:
                chunks.append(_chunk_dict(chunk_text, index, start, end))
                index += 1
            next_start = end - overlap if overlap > 0 else end
            if next_start <= start:
                next_start = end
            start = next_start
        return chunks

    def _split_by_tokens(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 100)
        overlap = params.get("chunk_overlap", 0)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        tokens = text.split()
        chunks: list[dict[str, Any]] = []
        index = 0
        for i in range(0, len(tokens), max(1, chunk_size - overlap)):
            end = min(i + chunk_size, len(tokens))
            chunk_text = " ".join(tokens[i:end])
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if len(chunk_text) >= min_chunk_size:
                chunks.append(_chunk_dict(chunk_text, index, i, end))
                index += 1
        return chunks

    def _split_by_sentences(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 5)
        overlap = params.get("chunk_overlap", 0)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", text) if s.strip()]
        chunks: list[dict[str, Any]] = []
        index = 0
        for i in range(0, len(sentences), max(1, chunk_size - overlap)):
            end = min(i + chunk_size, len(sentences))
            chunk_text = " ".join(sentences[i:end])
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if len(chunk_text) >= min_chunk_size:
                chunks.append(_chunk_dict(chunk_text, index, i, end))
                index += 1
        return chunks

    def _split_by_paragraphs(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 1)
        overlap = params.get("chunk_overlap", 0)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            return []
        chunks: list[dict[str, Any]] = []
        index = 0
        for i in range(0, len(paragraphs), max(1, chunk_size - overlap)):
            end = min(i + chunk_size, len(paragraphs))
            chunk_text = "\n\n".join(paragraphs[i:end])
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if len(chunk_text) >= min_chunk_size:
                chunks.append(_chunk_dict(chunk_text, index, i, end))
                index += 1
        return chunks

    def _split_by_lines(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 10)
        overlap = params.get("chunk_overlap", 0)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        lines = text.split("\n")
        chunks: list[dict[str, Any]] = []
        index = 0
        for i in range(0, len(lines), max(1, chunk_size - overlap)):
            end_idx = min(i + chunk_size, len(lines))
            chunk_text = "\n".join(lines[i:end_idx])
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if len(chunk_text) >= min_chunk_size:
                chunks.append(_chunk_dict(chunk_text, index, i, end_idx))
                index += 1
        return chunks

    def _split_by_regex(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        pattern = params.get("pattern")
        keep_separator = params.get("keep_separator", False)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        if not pattern:
            raise ValueError("Pattern is required for split_regex operation")
        if keep_separator:
            parts = re.split(f"({pattern})", text)
            merged: list[str] = []
            for i in range(0, len(parts) - 1, 2):
                merged.append(parts[i] + parts[i + 1])
            if parts and len(parts) % 2 == 1:
                merged.append(parts[-1])
            chunks_text = merged
        else:
            chunks_text = re.split(pattern, text)
        chunks: list[dict[str, Any]] = []
        for index, chunk_text in enumerate(chunks_text):
            if strip_chunks:
                chunk_text = chunk_text.strip()
            if not chunk_text or len(chunk_text) < min_chunk_size:
                continue
            chunks.append(_chunk_dict(chunk_text, index, 0, len(chunk_text)))
        return chunks

    def _smart_split(self, text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_size = params.get("chunk_size", 1000)
        strip_chunks = params.get("strip_chunks", True)
        min_chunk_size = params.get("min_chunk_size", 0)
        separators = params.get("separators") or [
            "\n## ",
            "\n### ",
            "\n#### ",
            "\n\n",
            "\n",
            ". ",
            "? ",
            "! ",
            "; ",
            ", ",
            " ",
        ]
        keep_separator = params.get("keep_separator", False)

        def split_recursive(text: str, seps: list[str], depth: int = 0) -> list[str]:
            if not text or len(text) <= chunk_size or not seps or depth > 5:
                return [text] if text else []
            sep = seps[0]
            parts = text.split(sep)
            if len(parts) <= 1:
                return split_recursive(text, seps[1:], depth + 1)
            chunks: list[str] = []
            current = ""
            for part in parts:
                candidate = current + (sep if keep_separator and sep else "") + part
                if len(candidate) <= chunk_size:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = part
            if current:
                chunks.append(current)
            out: list[str] = []
            for chunk in chunks:
                if len(chunk) > chunk_size:
                    out.extend(split_recursive(chunk, seps[1:], depth + 1))
                else:
                    out.append(chunk)
            return out

        pieces = split_recursive(text, separators)
        chunks: list[dict[str, Any]] = []
        index = 0
        offset = 0
        for piece in pieces:
            if strip_chunks:
                piece = piece.strip()
            if not piece or len(piece) < min_chunk_size:
                continue
            chunks.append(_chunk_dict(piece, index, offset, offset + len(piece)))
            offset += len(piece)
            index += 1
        if not chunks and text:
            chunks.append(_chunk_dict(text.strip() if strip_chunks else text, 0, 0, len(text)))
        return chunks
