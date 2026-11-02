# 功能：记忆维护工具——检索 / 展开明细 / 显式遗忘（项目 entries + long_term）
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bandi.memory.long_term import MemoryItem
from bandi.memory.memory_types import MemoryType
from bandi.memory.tbox import RELATION_TERMS, relation_for, validate_fact
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

logger = logging.getLogger(__name__)

# 支持显式遗忘的记忆层：long_term（SQLite 维度事实）/ episodic（经历）/
# working（会话 KV 快照）/ project_entry（entries/*.md）/ global（跨项目全局层）
_FORGET_SCOPES = ("long_term", "episodic", "working", "project_entry", "global")


def _markdown_for(db_dir: Path, context: ToolPermissionContext | None) -> Any:
    """功能：按运行时 project_root 解析项目级 markdown 记忆库（entries/）。

    project_root 经 ToolPermissionContext.extra 由 executor 注入（与生产
    MemoryManager 同一路径），保证工具读写的就是会话正在用的那份 /entries。
    """
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    root = str((getattr(context, "extra", None) or {}).get("project_root") or "").strip()
    return MarkdownMemoryStore(db_dir, project_root=root or None)


class MemorySearchTool(BaseTool):
    """功能：检索长期记忆与项目 entries——注入端只给指针/摘要，明细按需展开。"""

    name: str = "memory_search"
    description: str = (
        "Search long-term memory and project memory entries by keywords. Returns "
        "matched entries with scope, id, title and hook. Use memory_read to expand "
        "an entry in full before relying on its content."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords"},
            "limit": {"type": "integer", "description": "Max results (default 5)"},
        },
        "required": ["query"],
    }
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    # needs_context 时 executor 不注入 extra，检索只能命中全局层
    needs_context: bool = True

    def __init__(self, *, db_dir: Path | None = None, long_term: Any = None) -> None:
        super().__init__()
        self._db_dir = db_dir
        self._long_term = long_term

    @staticmethod
    def _dimensions(context: ToolPermissionContext | None) -> tuple[str | None, str | None]:
        """功能：从工具上下文取记忆维度（project_id / employee_id）。

        空串归一为 None —— 与存储层「未传维度」语义一致，避免退化成 `IS NULL` 命中。
        """
        extra = (getattr(context, "extra", None) or {}) if context is not None else {}
        project_id = str(extra.get("project_id") or "").strip() or None
        employee_id = str(extra.get("employee_id") or "").strip() or None
        return project_id, employee_id

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        limit = max(1, int(params.get("limit") or 5))
        project_id, employee_id = self._dimensions(context)
        hits: list[dict[str, Any]] = []
        if self._db_dir is not None:
            try:
                markdown = _markdown_for(self._db_dir, context)
                for pointer, score in markdown.search_entries(query, limit=limit):
                    hits.append(
                        {
                            "scope": "project_entry",
                            "id": pointer.entry_id,
                            "title": pointer.title,
                            "hook": pointer.hook,
                            "score": round(float(score), 4),
                        }
                    )
            except Exception as e:  # noqa: BLE001 — 单层损坏不阻断其余层
                logger.warning("memory_search project_entry failed: %s", e)
        if self._long_term is not None:
            try:
                long_term_items = list(
                    self._long_term.search_fts_ranked(
                        query, limit, project_id=project_id, employee_id=employee_id
                    )
                )
                for item in long_term_items:
                    hits.append(
                        {
                            "scope": "long_term",
                            "id": item.id,
                            "title": (item.content or "")[:60],
                            "hook": (item.content or "")[:200],
                            "importance": round(float(item.importance), 4),
                        }
                    )
                self._attach_entailed_facts(long_term_items, hits)
            except Exception as e:  # noqa: BLE001 — 单层损坏不阻断其余层
                logger.warning("memory_search long_term failed: %s", e)
        hits.sort(key=lambda h: float(h.get("score") or h.get("importance") or 0), reverse=True)
        return {"query": query, "count": len(hits[:limit]), "hits": hits[:limit]}

    def _attach_entailed_facts(self, items: list[Any], hits: list[dict[str, Any]]) -> None:
        """功能：把 long_term 命中的结构化三元组做连通分量闭包，派生结论写入对应
        hit 的 entailed_facts——只读派生（撤回前提即消失），失败静默降级。"""
        from bandi.memory.entailment import (
            expand_entailed_facts,
            fact_triple_from_metadata,
            render_entailed_fact,
        )

        seeds = []
        triples_by_id: dict[str, tuple[str, str, str]] = {}
        for item in items:
            triple = fact_triple_from_metadata(getattr(item, "metadata", None))
            if triple is not None:
                seeds.append(triple)
                triples_by_id[str(item.id)] = triple
        if not seeds:
            return
        derived = expand_entailed_facts(self._long_term, seeds)
        if not derived:
            return
        by_subject: dict[str, list[str]] = {}
        for entry in derived:
            by_subject.setdefault(str(entry.get("subject") or ""), []).append(
                render_entailed_fact(entry)
            )
        for hit in hits:
            triple = triples_by_id.get(str(hit.get("id") or ""))
            if triple is None:
                continue
            facts = by_subject.get(triple[0])
            if facts:
                hit["entailed_facts"] = facts


class MemoryReadTool(BaseTool):
    """功能：按 id 展开项目 entries 全文——索引化后明细的唯一读取入口。"""

    name: str = "memory_read"
    description: str = (
        "Read the full content of a project memory entry by its id "
        "(ids come from memory_search results or MEMORY.md index lines)."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string", "description": "Entry id returned by memory_search"},
        },
        "required": ["entry_id"],
    }
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def __init__(self, *, db_dir: Path | None = None) -> None:
        super().__init__()
        self._db_dir = db_dir

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        entry_id = str(params.get("entry_id") or "").strip()
        if not entry_id:
            return {"error": "entry_id is required"}
        if self._db_dir is None:
            return {"error": "memory store unavailable"}
        try:
            content = _markdown_for(self._db_dir, context).read_entry(entry_id)
        except Exception as e:  # noqa: BLE001 — 读取失败显式回传不抛断 agent 循环
            return {"error": f"read entry failed: {e}"}
        if not content:
            return {"error": f"entry not found: {entry_id}"}
        return {"entry_id": entry_id, "content": content}


class MemoryForgetTool(BaseTool):
    """功能：显式遗忘——删除指定记忆层的一条记录（不是等容量/TTL 被动淘汰）。"""

    name: str = "memory_forget"
    description: str = (
        "Explicitly forget a stored memory. Use when the user asks to forget "
        "something or a memory is wrong/sensitive. Contains five scopes: "
        "long_term (by id), episodic (by episode_id), working (by KV key, "
        "session-scoped), project_entry (deletes the entry file and its MEMORY.md "
        "index line), global (by key). Prefer passing id from memory_search; a query "
        "alone is accepted only when it matches exactly one record (never "
        "batch-deletes on ambiguity)."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": list(_FORGET_SCOPES),
                "description": "Which memory layer to forget from",
            },
            "id": {"type": "string", "description": "Target id (preferred)"},
            "query": {
                "type": "string",
                "description": "Keywords used to resolve a single target when id is absent",
            },
        },
        "required": ["scope"],
    }
    risk_level: str = "medium"
    is_destructive: bool = True
    is_concurrency_safe: bool = False
    # working scope 需要 session_id 定位会话 KV——executor 经 needs_context 注入
    needs_context: bool = True

    def __init__(
        self,
        *,
        db_dir: Path | None = None,
        long_term: Any = None,
        episodic: Any = None,
        working: Any = None,
        global_store: Any = None,
    ) -> None:
        super().__init__()
        self._db_dir = db_dir
        self._long_term = long_term
        self._episodic = episodic
        self._working = working
        self._global_store = global_store

    def _resolve_id(
        self,
        scope: str,
        entry_id: str,
        query: str,
        context: ToolPermissionContext | None,
        session_id: str = "",
    ) -> str:
        """功能：id 缺省时按 query 解析唯一目标——多命中即拒绝（不猜、不批量删）。"""
        if entry_id:
            return entry_id
        if not query:
            return ""
        if scope == "project_entry" and self._db_dir is not None:
            matches = _markdown_for(self._db_dir, context).search_entries(query, limit=2)
            return matches[0][0].entry_id if len(matches) == 1 else ""
        if scope == "long_term" and self._long_term is not None:
            matches = self._long_term.search_fts_ranked(query, 2)
            return matches[0].id if len(matches) == 1 else ""
        if scope == "episodic" and self._episodic is not None:
            matches = self._episodic.search(query, 2)
            return matches[0][0].episode_id if len(matches) == 1 else ""
        if scope == "working" and self._working is not None:
            matches = self._working.search(query, 2, session_id=session_id)
            return matches[0][0].key if len(matches) == 1 else ""
        if scope == "global" and self._global_store is not None:
            matches = self._global_store.search(query, 2)
            return str(matches[0]["key"]) if len(matches) == 1 else ""
        return ""

    @staticmethod
    def _forget_project_entry(markdown: Any, entry_id: str) -> bool:
        """功能：删除 entries/<id>.md 并同步移除 指针行（防死链）。"""
        from bandi.memory.markdown_memory import ENTRY_ID_RE

        if not ENTRY_ID_RE.match(entry_id or ""):
            raise ValueError(f"invalid entry id: {entry_id!r}")
        root = markdown.entries_dir.resolve()
        path = (root / f"{entry_id}.md").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"entry id escapes entries dir: {entry_id!r}")
        existed = bool(path.exists())
        if existed:
            path.unlink(missing_ok=True)
        text = markdown.load_visible_text()
        kept = [line for line in text.splitlines() if f"entries/{entry_id}.md" not in line]
        if len(kept) != len(text.splitlines()):
            markdown.save_visible_text("\n".join(kept))
        return existed

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        scope = str(params.get("scope") or "").strip()
        if scope not in _FORGET_SCOPES:
            return {
                "error": f"unsupported scope: {scope or '(empty)'}; allowed: {list(_FORGET_SCOPES)}"
            }
        session_id = str(((getattr(context, "extra", None) or {}).get("session_id")) or "")
        target_id = self._resolve_id(
            scope,
            str(params.get("id") or "").strip(),
            str(params.get("query") or "").strip(),
            context,
            session_id,
        )
        if not target_id:
            return {
                "forgotten": False,
                "reason": "target id unresolved (pass id, or a query matching exactly one entry)",
            }
        if scope == "project_entry":
            if self._db_dir is None:
                return {"error": "memory store unavailable"}
            try:
                deleted = self._forget_project_entry(
                    _markdown_for(self._db_dir, context), target_id
                )
            except Exception as e:  # noqa: BLE001 — 删除失败显式回传
                return {"error": f"forget project_entry failed: {e}"}
            return {"forgotten": deleted, "scope": scope, "id": target_id}
        if scope == "episodic":
            if self._episodic is None:
                return {"error": "memory store unavailable"}
            try:
                deleted = self._episodic.delete(target_id)
            except Exception as e:  # noqa: BLE001 — 删除失败显式回传
                return {"error": f"forget episodic failed: {e}"}
            return {"forgotten": deleted, "scope": scope, "id": target_id}
        if scope == "working":
            if self._working is None:
                return {"error": "memory store unavailable"}
            try:
                deleted = self._working.delete(target_id, session_id=session_id)
            except Exception as e:  # noqa: BLE001 — 删除失败显式回传
                return {"error": f"forget working failed: {e}"}
            return {"forgotten": deleted, "scope": scope, "id": target_id}
        if scope == "global":
            if self._global_store is None:
                return {"error": "memory store unavailable"}
            try:
                deleted = self._global_store.delete(target_id)
            except Exception as e:  # noqa: BLE001 — 删除失败显式回传
                return {"error": f"forget global failed: {e}"}
            return {"forgotten": deleted, "scope": scope, "id": target_id}
        if self._long_term is None:
            return {"error": "memory store unavailable"}
        try:
            self._long_term.delete(target_id)
        except Exception as e:  # noqa: BLE001 — 删除失败显式回传
            return {"error": f"forget long_term failed: {e}"}
        return {"forgotten": True, "scope": scope, "id": target_id}


class MemoryAssertTool(BaseTool):
    """功能：结构化写入记忆三元组——Agent 给三元组，代码只校验（不解析散文）。

    移植 dsh-ontology 的 `ontology_assert`（`src/tools.ts:285-333`）。该项目的核心
    设计是 **validate, not parse**：不给代码一个正则去猜实体，而是让模型显式给
    `subject/predicate/object`，代码校验三元组是否落在已声明的关系词汇表上；违规
    时把**可操作的错误消息**回给模型，让它自己改——而不是自动改写、也不是悄悄放过。
    """

    name: str = "memory_assert"
    description: str = (
        "Record a structured memory triple (subject/predicate/object). Use this when "
        "a durable fact ties two things together — a dependency, an owner, a blocker, "
        "a supersession — instead of writing it as prose. The predicate MUST come "
        "from the declared relation vocabulary; an undeclared predicate is rejected "
        "with a message telling you which predicates are available. Re-asserting the "
        "same triple is idempotent. This is for structure, not for 'remember this' "
        "style preferences — those are stored automatically."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "The subject (entity name)"},
            "predicate": {
                "type": "string",
                "enum": sorted(RELATION_TERMS),
                "description": "Declared relation id",
            },
            "object": {
                "type": "string",
                "description": "The object (entity name or literal value)",
            },
            "memory_type": {
                "type": "string",
                "enum": [str(t) for t in MemoryType],
                "description": "Memory type of this fact (default: business_fact)",
            },
        },
        "required": ["subject", "predicate", "object"],
    }
    risk_level: str = "low"
    is_read_only: bool = False
    needs_context: bool = True

    def __init__(self, *, long_term: Any = None, llm: Any = None) -> None:
        super().__init__()
        self._long_term = long_term
        self._llm = llm

    async def _canonical_subject(
        self, subject: str, project_id: str | None = None, employee_id: str | None = None
    ) -> str:
        """功能：把新出现的实体名归一到库中已有的规范名（委托 `normalize_entities` 编排）。

        不接这一步，「明远科技」与「明远科技公司」会算出两个三元组 ID，
        同一件事被存成两条且永不合并。归一失败（无 LLM / 判定不可靠 / 调用
        出错）时原样返回——宁可不合并，也不要错合并。
        """
        if self._llm is None or self._long_term is None or not subject:
            return subject
        from bandi.memory.entity_normalization import normalize_entities

        try:
            plan = await normalize_entities(
                [subject],
                self._existing_subjects(project_id=project_id, employee_id=employee_id),
                llm=self._llm,
            )
        except Exception:  # noqa: BLE001 — 归一是增强项，失败不阻断写入
            return subject
        for new, canonical in plan.merges:
            if new == subject:
                return canonical
        return subject

    def _existing_subjects(
        self, project_id: str | None = None, employee_id: str | None = None
    ) -> list[str]:
        """功能：列出同 scope 库里已出现过的实体名——归一候选的「已有名字」一侧。"""
        names: list[str] = []
        for item in self._long_term.list_items(project_id=project_id, employee_id=employee_id):
            meta = getattr(item, "metadata", None) or {}
            name = str(meta.get("subject") or "").strip()
            if name:
                names.append(name)
        return list(dict.fromkeys(names))

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        subject = str(params.get("subject") or "").strip()
        predicate = str(params.get("predicate") or "").strip()
        object_value = str(params.get("object") or "").strip()
        memory_type = str(params.get("memory_type") or "").strip() or str(MemoryType.BUSINESS_FACT)

        violations = validate_fact(
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            subject_type=memory_type,
        )
        if violations:
            # 拒绝写入 + 把违规原因回给模型，让它下一轮自己改（dsh-ontology 纪律）
            return {
                "asserted": False,
                "violations": [{"code": v.code, "message": v.message} for v in violations],
                "hint": "fix the input and retry; do not work around the vocabulary",
            }
        if self._long_term is None:
            return {"error": "memory store unavailable"}

        project_id, employee_id = MemorySearchTool._dimensions(context)
        canonical_subject = await self._canonical_subject(
            subject, project_id=project_id, employee_id=employee_id
        )
        normalized = canonical_subject != subject

        term = relation_for(predicate)
        assert term is not None  # validate_fact 已保证
        content = f"{canonical_subject}·{term.label}·{object_value}"
        item = MemoryItem(
            item_id=f"triple:{_triple_id(canonical_subject, predicate, object_value)}",
            content=content,
            importance=0.6,
            metadata={
                "memory_type": memory_type,
                "description": f"关系·{canonical_subject}{term.label}{object_value}",
                "subject": canonical_subject,
                "predicate": predicate,
                "object": object_value,
                "source": "memory_assert",
                **({"normalized_from": subject} if normalized else {}),
            },
        )
        try:
            self._long_term.store(item, project_id=project_id, employee_id=employee_id)
        except Exception as e:  # noqa: BLE001 — 写失败显式回传，不静默
            return {"error": f"memory_assert store failed: {e}"}
        return {
            "asserted": True,
            "id": item.id,
            "content": content,
            "symmetric": term.symmetric,
            "transitive": term.transitive,
            **({"normalized_from": subject} if normalized else {}),
        }


def _triple_id(subject: str, predicate: str, object_value: str) -> str:
    """功能：三元组稳定 ID——同一条三元组重复断言覆盖而非堆积。"""
    import hashlib

    digest = hashlib.sha256(f"{subject}|{predicate}|{object_value}".encode()).hexdigest()[:12]
    return digest
