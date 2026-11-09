# 功能：KnowledgeSearchTool — 知识库渐进检索（catalog → search → read）
# catalog=列记忆项摘要 / search=FTS5 命中 / read=按 item_id 读内容窗口

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any

from bandi.knowledge.scopes import (
    get_runtime_knowledge_bases,
    get_runtime_knowledge_employee,
)
from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

if TYPE_CHECKING:
    from bandi.knowledge.store import KnowledgeStore
    from bandi.memory.long_term import LongTermMemory

_DEFAULT_SEARCH_LIMIT = 8
_MAX_SEARCH_LIMIT = 20
_DEFAULT_READ_CHARS = 8000
_MAX_READ_CHARS = 32_000
_MAX_CATALOG = 100
_CATALOG_SUMMARY_CHARS = 200
_CITATION_LIMIT = 4
_NO_EVIDENCE_NOTICE = (
    "本轮无检索依据：知识库与长期记忆均未命中，不得编造引用内容；"
    "如无法基于已有上下文回答，请向用户说明不确定"
)
_QUERY_EDGE_CHARS = " \t\r\n\u3000\"'“”‘’「」『』？?。，,．.！!；;：:、"


def normalize_query(raw: str) -> str:
    """功能：检索入口轻量规范化（全半角归一 + 边缘引号标点剥离 + 空白折叠），纯函数。"""
    text = unicodedata.normalize("NFKC", str(raw)).strip(_QUERY_EDGE_CHARS)
    return " ".join(text.split())


def _lazy_memory(memory: LongTermMemory | None) -> LongTermMemory:
    if memory is not None:
        return memory
    from bandi.memory.long_term import LongTermMemory

    return LongTermMemory()


class KnowledgeSearchTool(FsTool):
    name: str = "knowledge_search"
    # 安全（③⁻）：知识条目内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "User knowledge base (catalog → search → read): catalog lists stored knowledge items "
        "with summaries, search runs BM25/FTS over item content, read returns a text window "
        "for a specific item id. Call action=catalog first, then action=search, then "
        "action=read with the returned item ids."
    )
    category: ToolCategory = ToolCategory.DOC
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["catalog", "search", "read"],
                "description": "catalog: list items with summaries; search: FTS query (requires query); read: text window (requires item_id)",
            },
            "query": {"type": "string", "description": "Search query (required for action=search)"},
            "item_id": {
                "type": "string",
                "description": "Knowledge item id (required for action=read)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_SEARCH_LIMIT,
                "description": f"Max results (1-{_MAX_SEARCH_LIMIT}, default {_DEFAULT_SEARCH_LIMIT})",
            },
            "start_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Inclusive start offset into content (read)",
            },
            "end_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Exclusive end offset into content (read)",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_READ_CHARS,
                "description": f"Max characters from start_offset when end_offset omitted (default {_DEFAULT_READ_CHARS})",
            },
        },
        "required": ["action"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    def __init__(
        self,
        memory: Any | None = None,
        knowledge_store: KnowledgeStore | None = None,
    ) -> None:
        super().__init__()
        self._memory = memory
        self._knowledge_store = knowledge_store

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        action = str(params.get("action") or "").strip()
        if action not in ("catalog", "search", "read"):
            return {"success": False, "error": "action must be catalog, search, or read"}
        try:
            memory = _lazy_memory(self._memory)
            # OrgRole.permissions["knowledge_base"] 写入）——None=不过滤，[]=全禁
            allowed_base_ids = get_runtime_knowledge_bases()
            employee_id = get_runtime_knowledge_employee()
            extra = (getattr(context, "extra", None) or {}) if context is not None else {}
            project_id = str(extra.get("project_id") or "").strip() or None
            if action == "catalog":
                return self._catalog(memory, params, allowed_base_ids, employee_id, project_id)
            if action == "search":
                return self._search(memory, params, allowed_base_ids, employee_id, project_id)
            return self._read(memory, params, allowed_base_ids, employee_id, project_id)
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _store(self) -> KnowledgeStore | None:
        """功能：惰性回填共享知识库存储（进程级单例——与 ingest 工具同库）。"""
        if self._knowledge_store is not None:
            return self._knowledge_store
        from bandi.knowledge.store import get_shared_store

        self._knowledge_store = get_shared_store()
        return self._knowledge_store

    def _catalog(
        self,
        memory: Any,
        params: dict[str, Any],
        allowed_base_ids: list[str] | None = None,
        employee_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        documents: list[dict[str, Any]] = []
        store = self._store()
        if store is not None:
            documents = [
                {
                    "item_id": doc.id,
                    "source": "knowledge_base",
                    "filename": doc.filename,
                    "title": doc.title,
                    "bucket_count": doc.bucket_count,
                    "chunk_count": doc.chunk_count,
                    "timestamp": doc.updated_at,
                }
                for doc in self._visible_documents(store, allowed_base_ids, employee_id)[
                    :_MAX_CATALOG
                ]
            ]
        try:
            items = memory.retrieve(
                "", limit=_MAX_CATALOG, project_id=project_id, employee_id=employee_id
            )
        except TypeError:  # 测试/自定义 memory 未实现 scope 参数时按旧签名回退
            items = memory.retrieve("", limit=_MAX_CATALOG)
        documents.extend(
            {
                "item_id": item.id,
                "source": "long_term",
                "summary": item.content[:_CATALOG_SUMMARY_CHARS],
                "importance": item.importance,
                "timestamp": item.timestamp,
            }
            for item in items
        )
        if not documents:
            return {
                "success": True,
                "action": "catalog",
                "total": 0,
                "documents": [],
                "hint": "knowledge base is empty; store items via long-term memory first",
            }
        return {
            "success": True,
            "action": "catalog",
            "total": len(documents),
            "documents": documents,
            "hint": "next: action=search with a focused query, then action=read for an item_id window",
        }

    @staticmethod
    def _visible_documents(
        store: KnowledgeStore, base_ids: list[str] | None, employee_id: str | None
    ) -> list[Any]:
        # 功能：可见文档 = 库可见域 ∩ 员工归属（节点未指定员工时对所有员工可见）。
        documents = store.list_all_documents(base_ids=base_ids)
        allowed_bases = set(base_ids) if base_ids is not None else None
        base_owners = {
            base.id: str(getattr(base, "employee_id", "") or "")
            for base in store.list_bases()
            if allowed_bases is None or base.id in allowed_bases
        }
        visible = []
        for doc in documents:
            base_owner = base_owners.get(doc.base_id, "")
            if base_owner and base_owner != (employee_id or ""):
                continue
            node_owner = str(getattr(doc, "node_employee_id", "") or "")
            if node_owner and node_owner != (employee_id or ""):
                continue
            visible.append(doc)
        return visible

    def _search(
        self,
        memory: Any,
        params: dict[str, Any],
        allowed_base_ids: list[str] | None = None,
        employee_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        query = normalize_query(str(params.get("query") or ""))
        if not query:
            return {
                "success": False,
                "error": "query is required for action=search",
                "action": "search",
            }
        limit = int(params.get("limit") or _DEFAULT_SEARCH_LIMIT)
        limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
        results: list[dict[str, Any]] = []
        store = self._store()
        if store is not None:
            from bandi.knowledge.okf import search_concepts, selected_concept_cards

            visible_documents = self._visible_documents(store, allowed_base_ids, employee_id)
            allowed_document_ids = {document.id for document in visible_documents}
            concepts = search_concepts(
                query, store.list_all_concepts(base_ids=allowed_base_ids), limit=3
            )
            if employee_id:
                concepts = [
                    concept
                    for concept in concepts
                    if str(getattr(concept, "document_id", "")) in allowed_document_ids
                ]
            concept_cards = selected_concept_cards(concepts)
            results.extend(
                {
                    "item_id": card["id"],
                    "source": "okf_concept",
                    "title": card["title"],
                    "content": str(card.get("content") or "")[:_CATALOG_SUMMARY_CHARS],
                    "concept_id": card["concept_id"],
                }
                for card in concept_cards
            )
            # 知识库 chunk 命中次之（结构化三层，带定位链）——末尾按 limit 统一截断
            document_titles = {
                document.id: str(document.title or "") for document in visible_documents
            }
            results.extend(
                {
                    "item_id": chunk.id,
                    "source": "knowledge_base",
                    "content": chunk.content[:_CATALOG_SUMMARY_CHARS],
                    "document_id": chunk.document_id,
                    "bucket_id": chunk.bucket_id,
                    "source_ref": chunk.source_ref,
                    "document_title": document_titles.get(chunk.document_id, ""),
                }
                for chunk in store.search_chunks(query, limit=limit, base_ids=allowed_base_ids)
                if chunk.document_id in allowed_document_ids
            )
        try:
            hits = memory.search_fts(
                query, limit=limit, project_id=project_id, employee_id=employee_id
            )
        except TypeError:  # 测试/自定义 memory 未实现 scope 参数时按旧签名回退
            hits = memory.search_fts(query, limit=limit)
        results.extend(
            {
                "item_id": hit.id,
                "source": "long_term",
                "importance": hit.importance,
                "content": hit.content[:_CATALOG_SUMMARY_CHARS],
            }
            for hit in hits
        )
        results = results[:limit]
        citations = self._build_citations(results)
        output: dict[str, Any] = {
            "success": True,
            "action": "search",
            "query": query,
            "total": len(results),
            "results": results,
            "evidence_status": "hit" if results else "none",
            "hint": "use action=read with item_id to load the full text window"
            if results
            else _NO_EVIDENCE_NOTICE,
        }
        if citations:
            # 10.6：知识库命中附带引用标签（[n] ↔ chunk/文档溯源链）——回答侧
            # 可引用 [n]，每条结论可回溯到具体 chunk
            output["citations"] = citations
        # 功能：检索范围（中文库名）——前端过程块显示「调用了哪个知识库」
        if store is not None and allowed_base_ids is not None:
            try:
                names = [
                    base.name
                    for base in store.list_bases()
                    if base.id in set(allowed_base_ids) and str(base.name or "").strip()
                ]
            except Exception:  # noqa: BLE001 — 展示用元数据，失败不影响检索
                names = []
            output["scope"] = {"base_ids": list(allowed_base_ids), "base_names": names}
        return output

    @staticmethod
    def _build_citations(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """功能（10.6）：knowledge_base chunk 命中 → 引用列表（identity 去重 +
        [n] 编号 + chunk_id/document_id 溯源）。无知识库命中返回空列表。"""
        evidence_pack = [
            {
                "content": str(item.get("content") or ""),
                "excerpt": str(item.get("content") or ""),
                "summary": str(item.get("content") or ""),
                "title": str(item.get("document_title") or ""),
                "source_path": str(item.get("source_ref") or item.get("document_id") or ""),
                "chunk_id": str(item.get("item_id") or ""),
                "document_id": item.get("document_id"),
                "bucket_id": item.get("bucket_id"),
            }
            for item in results
            if item.get("source") == "knowledge_base"
        ]
        if not evidence_pack:
            return []
        from bandi.knowledge.citations import knowledge_citations_from_results

        return knowledge_citations_from_results(
            [{"evidence_pack": evidence_pack}], limit=_CITATION_LIMIT
        )

    def _read(
        self,
        memory: Any,
        params: dict[str, Any],
        allowed_base_ids: list[str] | None = None,
        employee_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        item_id = str(params.get("item_id") or "").strip()
        if not item_id:
            return {
                "success": False,
                "error": "item_id is required for action=read",
                "action": "read",
            }
        item = None
        scope_of = getattr(memory, "scope_of", None)
        if callable(scope_of):
            scope = scope_of(item_id)
            project_ok = not scope or not scope[0] or scope[0] == project_id
            employee_ok = not scope or not scope[1] or scope[1] == employee_id
            if project_ok and employee_ok:
                item = memory.get(item_id)
        else:
            item = memory.get(item_id)
        content: str | None = item.content if item is not None else None
        if content is None:
            store = self._store()
            visible_ids: set[str] = set()
            if store is not None:
                visible_ids = {
                    doc.id for doc in self._visible_documents(store, allowed_base_ids, employee_id)
                }
            # chunk id（kchunk:...）
            chunk = store.get_chunk(item_id) if store is not None else None
            if (
                chunk is not None
                and allowed_base_ids is not None
                and (chunk.base_id not in allowed_base_ids)
            ):
                chunk = None
            if chunk is not None and chunk.document_id not in visible_ids:
                chunk = None
            if chunk is not None and employee_id:
                document = store.get_document(chunk.document_id) if store is not None else None
                if document is not None and document.node_employee_id not in (
                    None,
                    "",
                    employee_id,
                ):
                    chunk = None
            content = chunk.content if chunk is not None else None
            # catalog/search 返回的文档 id（kdoc:...）——按文档全部 chunk 拼接
            document = (
                store.get_document(item_id) if store is not None and content is None else None
            )
            if document is not None and document.id in visible_ids:
                chunks = store.list_chunks(document_id=document.id) if store is not None else []
                content = "\n\n".join(chunk.content for chunk in chunks).strip() or None
            # 概念卡 id（kcpt:...）
            if content is None and store is not None:
                concept = next(
                    (
                        row
                        for row in store.list_all_concepts(base_ids=allowed_base_ids)
                        if row.id == item_id or row.concept_id == item_id
                    ),
                    None,
                )
                if concept is not None and (
                    concept.document_id in visible_ids or not concept.document_id
                ):
                    content = concept.content_md
        if content is None:
            return {"success": False, "error": f"item not found: {item_id}", "action": "read"}
        start = int(params.get("start_offset") or 0)
        start = max(0, min(start, len(content)))
        end_raw = params.get("end_offset")
        if end_raw is not None:
            end = max(start, min(int(end_raw), len(content)))
        else:
            max_chars = int(params.get("max_chars") or _DEFAULT_READ_CHARS)
            max_chars = max(1, min(max_chars, _MAX_READ_CHARS))
            end = min(len(content), start + max_chars)
        truncated = end < len(content)
        return {
            "success": True,
            "action": "read",
            "item_id": item_id,
            "importance": item.importance if item is not None else None,
            "start_offset": start,
            "end_offset": end,
            "total_chars": len(content),
            "truncated": truncated,
            "text": content[start:end],
            "hint": "increase start_offset or call again with a later window"
            if truncated
            else None,
        }
