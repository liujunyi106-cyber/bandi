# 功能：知识库三层实体（document → bucket → chunk）——参考实现 knowledge/schema

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeDocument:
    id: str
    base_id: str
    filename: str
    title: str = ""
    status: str = "ready"
    bucket_count: int = 0
    chunk_count: int = 0
    node_employee_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class KnowledgeBase:
    """功能：知识库容器（base 级 scope——employee_id/project_id 归属，None = 全局）。"""

    id: str
    name: str
    employee_id: str | None = None
    project_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class KnowledgeBucket:
    id: str
    base_id: str
    document_id: str
    bucket_key: str
    title: str
    summary: str = ""
    chunk_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class KnowledgeChunk:
    id: str
    base_id: str
    document_id: str
    bucket_id: str
    chunk_index: int
    content: str
    summary: str = ""
    source_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class KnowledgeConcept:
    id: str
    base_id: str
    document_id: str = ""
    concept_id: str = ""
    concept_type: str = "Topic"
    title: str = ""
    description: str = ""
    content_md: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class IngestResult:
    document_id: str
    bucket_count: int
    chunk_count: int
