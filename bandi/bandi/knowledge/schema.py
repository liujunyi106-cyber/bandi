# 功能：知识库数据模型——文档、知识桶、段落块、概念与摄取结果的字段定义。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeDocument:
    """功能：一篇已入库文档（base → document → bucket → chunk 的第一层）。"""

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
    """功能：知识库容器，按员工或项目划归属（两者皆空即全局库）。"""

    id: str
    name: str
    employee_id: str | None = None
    project_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class KnowledgeBucket:
    """功能：文档内按标题切出的知识桶，是段落块的分组单位。"""

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
    """功能：检索与引用的最小单位，承载一段原文及其定位信息。"""

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
    """功能：OKF 概念——由文档/知识桶派生出的可引用知识条目。"""

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
    """功能：一次摄取的统计结果。"""

    document_id: str
    bucket_count: int
    chunk_count: int
