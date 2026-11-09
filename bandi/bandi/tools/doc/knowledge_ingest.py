# 功能：KnowledgeIngestTool — 知识库文档投放工具（ingest 生产入口①）。
# 一次调用 = 三层投影落库 + OKF 概念自动构建（build_okf_for_document → upsert_concepts）；
# 写工具（is_read_only=False），风险 medium——写面仅限知识库沙箱库，不触文件系统。

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bandi.knowledge.okf import build_okf_for_document
from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

if TYPE_CHECKING:
    from bandi.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgeIngestTool(FsTool):
    name: str = "knowledge_ingest"
    description: str = (
        "Ingest a document into the employee knowledge base: stores the document, "
        "splits it into titled buckets (Markdown headings) and searchable chunks, and "
        "auto-builds OKF concept cards. Requires base_id, filename and text."
    )
    category: ToolCategory = ToolCategory.DOC
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "base_id": {
                "type": "string",
                "description": "Knowledge base id (created on first use; e.g. 'employee:ops' or project id)",
            },
            "filename": {
                "type": "string",
                "description": "Document filename (unique per base; re-ingest replaces the previous version)",
                "aliases": ["file", "name"],
            },
            "text": {
                "type": "string",
                "description": "Full document text (Markdown headings become buckets)",
                "aliases": ["content", "body"],
            },
            "title": {"type": "string", "description": "Optional display title"},
            "employee_id": {
                "type": "string",
                "description": "Optional owner employee scope for the base",
            },
        },
        "required": ["base_id", "filename", "text"],
    }
    param_aliases: dict[str, str] = {
        "file": "filename",
        "name": "filename",
        "content": "text",
        "body": "text",
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = False
    risk_level: str = "medium"

    def __init__(self, knowledge_store: KnowledgeStore | None = None) -> None:
        super().__init__()
        self._knowledge_store = knowledge_store

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        base_id = str(params.get("base_id") or "").strip()
        filename = str(params.get("filename") or "").strip()
        text = str(params.get("text") or "")
        title = str(params.get("title") or "").strip()
        employee_id = str(params.get("employee_id") or "").strip() or None
        if not base_id:
            return {"success": False, "error": "base_id is required"}
        if not filename:
            return {"success": False, "error": "filename is required"}
        if not text.strip():
            return {"success": False, "error": "text is blank"}
        try:
            store = self._store()
            store.ensure_base(base_id, employee_id=employee_id)
            result = store.ingest_document(
                base_id=base_id, filename=filename, text=text, title=title
            )
            document = store.get_document(result.document_id)
            buckets = store.list_buckets(result.document_id)
            concepts = store.upsert_concepts(base_id, build_okf_for_document(document, buckets))
            try:
                from bandi.resources.knowledge_sources import write_source

                write_source(base_id, filename, text)
            except Exception as exc:  # noqa: BLE001 - 源文件是辅助面，不阻断摄取
                logger.warning("knowledge source write failed: %s", exc)
            return {
                "success": True,
                "document_id": result.document_id,
                "base_id": base_id,
                "bucket_count": result.bucket_count,
                "chunk_count": result.chunk_count,
                "concept_count": len(concepts),
                "hint": "use knowledge_search action=search to verify retrieval",
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _store(self) -> KnowledgeStore:
        """功能：惰性回填共享知识库存储（与 knowledge_search 同一单例）。"""
        if self._knowledge_store is not None:
            return self._knowledge_store
        from bandi.knowledge.store import get_shared_store

        self._knowledge_store = get_shared_store()
        return self._knowledge_store
