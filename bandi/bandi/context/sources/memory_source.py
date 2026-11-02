# 功能：记忆上下文源 — 检索记忆注入感知层（MemoryContextSource）

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bandi.context.item import ContextItem
from bandi.context.source import RuntimeContextSource
from bandi.core.message import Message
from bandi.memory.episodic import tokenize_cn
from bandi.memory.routing import classify_memory_query
from bandi.utils.token_counter import TokenCounter

if TYPE_CHECKING:
    from bandi.memory.memory_provider import MemoryManager

logger = logging.getLogger(__name__)

_MEMORY_ITEM_LIMIT = 5
_MEMORY_ITEM_CHARS = 200
_ENRICHED_ENTITY_LIMIT = 3
# AR-17（ADJ-04 方案 1 过渡）：focused 规则打分——标题命中 ×3、正文命中 ×1、警示词 +0.5
_FOCUSED_HEADING_WEIGHT = 3.0
_FOCUSED_BODY_WEIGHT = 1.0
_FOCUSED_WARNING_BONUS = 0.5
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "to", "and", "or", "is", "are", "的", "了", "和", "是", "在", "我"}
)
_WARNING_HINTS = (
    "checklist",
    "warning",
    "watchout",
    "gotcha",
    "risk",
    "注意",
    "警告",
    "坑",
    "风险",
    "清单",
)


def _query_terms(query: str) -> list[str]:
    """功能：查询分词（按非中英文数字切分 + 停用词过滤），供 focused 打分。
    片段复用 tokenize_cn 切 2~4 字 n-gram——整句成词会使中文查询零区分度。"""
    terms: list[str] = []
    for part in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", query.lower()):
        if not part or part in _STOPWORDS:
            continue
        terms.extend(tokenize_cn(part) or [part])
    return terms


def _focused_score(entry: object, terms: list[str]) -> float:
    """功能：focused 规则打分——标题面（key/title）命中 ×3、正文命中 ×1
    警示词（清单/警告/坑/风险）命中共 +0.5（ADJ-04 方案 1）。"""
    metadata = getattr(entry, "metadata", None) or {}
    key = str(getattr(entry, "key", "") or "")
    title = str(metadata.get("title") or "")
    heading = f"{key} {title}".lower()
    body = str(getattr(entry, "value", "") or "").lower()
    score = 0.0
    for term in terms:
        if term in heading:
            score += _FOCUSED_HEADING_WEIGHT
        elif term in body:
            score += _FOCUSED_BODY_WEIGHT
    if any(hint in heading or hint in body for hint in _WARNING_HINTS):
        score += _FOCUSED_WARNING_BONUS
    return score


def _seen_store_path() -> Path:
    # 功能：seen 状态持久化路径惰性求值——Path.home patch（测试隔离）必须生效；
    return Path.home() / ".bandi" / "seen_memory.json"


_SEEN_MAX_KEYS_PER_SESSION = 500
_SEEN_MAX_SESSIONS = 50


def _enrichment_note(metadata: dict) -> str:
    """功能：本体增强后缀——相关实体（去重、上限 3，仅取 str 项）与矛盾提示；
    供 collect 注入消费（此前后端增强产物只写 metadata、注入链不可见）。"""
    note = ""
    entities = metadata.get("enriched_entities")
    if isinstance(entities, (list, tuple)):
        names: list[str] = []
        seen: set[str] = set()
        for raw in entities:
            if not isinstance(raw, str):
                continue
            name = raw.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= _ENRICHED_ENTITY_LIMIT:
                break
        if names:
            note += f"（相关实体：{'、'.join(names)}）"
    if metadata.get("contradiction_with"):
        note += "（与另一条记忆存在矛盾）"
    return note


def _freshness_note(metadata: dict) -> str:
    """功能：陈旧经历新鲜度警告（>1 天，cc-haha memoryAge 语义）——保留记忆但
    提醒可能过时，替代单纯降权淘汰。"""
    ts = metadata.get("created_at") or metadata.get("timestamp")
    if not ts:
        return ""
    try:
        born = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return ""
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    days = int((datetime.now(UTC) - born).total_seconds() // 86400)
    if days <= 1:
        return ""
    return f"（记录于 {days} 天前，可能已过时）"


# memory_source 检索时直接剔除（同一轮内已注入全文，再注入 = 纯重复）；
# 与 SEEN（跨轮降权）职责分离
_HIERARCHY_LOCK = threading.Lock()
HIERARCHY_INJECTED_BY_SESSION: dict[str, set[str]] = {}

_HIERARCHY_MAX_SESSIONS = 50


def _trim_hierarchy_injected() -> None:
    """功能：超限淘汰最旧会话（dict 保序）——调用方须已持 _HIERARCHY_LOCK。"""
    while len(HIERARCHY_INJECTED_BY_SESSION) > _HIERARCHY_MAX_SESSIONS:
        oldest = next(iter(HIERARCHY_INJECTED_BY_SESSION))
        HIERARCHY_INJECTED_BY_SESSION.pop(oldest, None)


# 每 turn 新建 MemoryContextSource，实例态跨轮即失——用模块级容器作为跨轮宿主：
# {session_id: {memory_id: count}}。持久化到 _SEEN_STORE_PATH（重启恢复）。
_SEEN_LOCK = threading.Lock()
SEEN_BY_SESSION: dict[str, dict[str, int]] = {}


def load_seen_state() -> None:
    """功能：从持久化文件恢复 seen 状态（模块加载/重启后调用）。"""
    global SEEN_BY_SESSION
    try:
        path = _seen_store_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            loaded = {
                str(sid): {str(k): int(v) for k, v in items.items()}
                for sid, items in data.items()
                if isinstance(items, dict)
            }
            with _SEEN_LOCK:
                SEEN_BY_SESSION.update(loaded)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("seen memory state load failed; starting empty")


def persist_seen_state() -> None:
    """功能：原子写 seen 状态（注入后调用；失败静默不阻断主链路）。"""
    try:
        path = _seen_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SEEN_LOCK:
            payload = {sid: items for sid, items in SEEN_BY_SESSION.items() if items}
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError as e:
        logger.warning("seen memory state persist failed: %s", e)


def _memory_id(category: str, key: str) -> str:
    # 功能：统一 memory_id（category 命名空间）——跨分类同 key 不互覆
    return f"{category}:{key}"


load_seen_state()


class MemoryContextSource(RuntimeContextSource):
    """从 MemoryManager 检索与当前输入相关的记忆，作为感知层上下文注入。

    排到未注入记忆之后（嵌入式降权）——高相关仍可召回，但减少重复占位；被预算
    裁剪掉的候选不计入 seen（只有真正注入的才降权，避免误判）。
    """

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        project_id: str | None = None,
        employee_id: str | None = None,
        conversation_id: str | None = None,
        dm_conversation_id: str | None = None,
    ) -> None:
        self._manager = memory_manager
        self._project_id = project_id
        self._employee_id = employee_id
        self._conversation_id = conversation_id
        self._dm_conversation_id = dm_conversation_id

    async def collect(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]:
        if self._manager is None or not message.content:
            return []
        query = message.content
        question = classify_memory_query(query)
        # 本 source 只做 ContextItem 包装与预算裁剪
        entries = self._manager.search_unified(
            query,
            session_id=session_id,
            project_id=self._project_id,
            employee_id=self._employee_id,
            conversation_id=self._conversation_id,
            dm_conversation_id=self._dm_conversation_id,
            limit=_MEMORY_ITEM_LIMIT,
            scoring="fusion",
            intent_hint=intent_hint,
            question_hint=question,
        )
        with _HIERARCHY_LOCK:
            hierarchy_injected = HIERARCHY_INJECTED_BY_SESSION.get(session_id, frozenset())
        if hierarchy_injected:
            entries = [
                e
                for e in entries
                if _memory_id(str((e.metadata or {}).get("category") or "long_term"), e.key)
                not in hierarchy_injected
            ]
        with _SEEN_LOCK:
            seen_counts = {
                _memory_id(str((e.metadata or {}).get("category") or "long_term"), e.key): (
                    SEEN_BY_SESSION.get(session_id, {}).get(
                        _memory_id(str((e.metadata or {}).get("category") or "long_term"), e.key), 0
                    )
                )
                for e in entries
            }
        # 再 seen 降权分组（稳定排序保持组内分数序）
        terms = _query_terms(query)
        entries.sort(key=lambda e: _focused_score(e, terms), reverse=True)
        # 已注入过的排后（组内保持原顺序）。已注入项仍保留（可召回），只降权。
        entries.sort(
            key=lambda e: (
                seen_counts.get(
                    _memory_id(str((e.metadata or {}).get("category") or "long_term"), e.key), 0
                )
                > 0
            )
        )
        items: list[ContextItem] = []
        for entry in entries:
            value = entry.value or ""
            if not value:
                continue
            metadata = entry.metadata or {}
            snippet = (value + _enrichment_note(metadata))[:_MEMORY_ITEM_CHARS]
            category = str(metadata.get("category") or "long_term")
            # 4.8：陈旧经历注入新鲜度警告（>1 天）——保留记忆但提示可能过时
            note = _freshness_note(metadata) if category == "episodic" else ""
            # 4.4：project_md 注入附 memory_read 展开指针——200 字预览不再是无出口残片
            hint = ""
            if category == "project_md":
                entry_id = str(metadata.get("entry_id") or "")
                if entry_id:
                    hint = f"（原文可用 memory_read 展开：entry_id={entry_id}）"
            content = f"[{category}] {note}{snippet}{hint}"
            items.append(
                ContextItem(
                    id=f"memory:{category}:{entry.key}",
                    source_type="memory",
                    content=content,
                    priority=3,
                    token_estimate=TokenCounter.estimate(snippet),
                    metadata={"category": category, "key": entry.key},
                )
            )
        if budget and budget > 0:
            kept: list[ContextItem] = []
            used = 0
            for item in items:
                est = item.token_estimate
                if est > budget - used:
                    break
                kept.append(item)
                used += est
            items = kept
        injected = items[: _MEMORY_ITEM_LIMIT * 3]
        # 只对真正注入的候选递增跨轮 seen（裁剪后，避免计入被预算裁掉的）
        if injected:
            with _SEEN_LOCK:
                per_session = SEEN_BY_SESSION.pop(session_id, None) or {}
                SEEN_BY_SESSION[session_id] = per_session
                for item in injected:
                    category = str((item.metadata or {}).get("category") or "long_term")
                    key = str((item.metadata or {}).get("key") or "")
                    if key:
                        mid = _memory_id(category, key)
                        per_session[mid] = per_session.get(mid, 0) + 1
                # 容量上限：超限裁剪最旧（dict 保序，截断头部）
                if len(per_session) > _SEEN_MAX_KEYS_PER_SESSION:
                    for stale in list(per_session)[: len(per_session) - _SEEN_MAX_KEYS_PER_SESSION]:
                        per_session.pop(stale, None)
                if len(SEEN_BY_SESSION) > _SEEN_MAX_SESSIONS:
                    for stale_sid in list(SEEN_BY_SESSION)[
                        : len(SEEN_BY_SESSION) - _SEEN_MAX_SESSIONS
                    ]:
                        SEEN_BY_SESSION.pop(stale_sid, None)
            persist_seen_state()
        return injected
