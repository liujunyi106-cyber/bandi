# 功能：ContextLoader — 加载会话上下文（历史 + 实体 + 项目级指令 + 记忆），实现 RuntimeContextSource

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.context.source import RuntimeContextSource
from bandi.core.message import Message, MessageRole
from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX
from bandi.memory.session.store import SessionStore

logger = logging.getLogger(__name__)

_PROJECT_INSTRUCTION_FILES: list[str] = ["AGENTS.md", "CLAUDE.md"]

# key=`entry:{entry_id}` 对应）——标记已注入，memory_source 后续轮降权排后
_ENTRY_REF_RE = re.compile(r"entries/([^/)\s]+)\.md")

# 「[上下文摘要]」SYSTEM 时，「## 会话记忆」段的压缩纪要为同源重复注入
_COMPACTION_NOTE_RE = re.compile(r"\[压缩纪要 turn \d+\][\s\S]*?(?=\n\[压缩纪要 turn \d+\]|\Z)")


def _strip_compaction_notes(text: str) -> str:
    """功能：剔除压缩纪要段（多条逐段）——返回去重后文本。"""
    return _COMPACTION_NOTE_RE.sub("", text).strip()


def _history_has_compaction_summary(history: list[Message]) -> bool:
    """功能：历史中是否已有压缩摘要 SYSTEM 消息（去重判据）。"""
    for m in history:
        if m.role == MessageRole.SYSTEM and (m.content or "").lstrip().startswith(
            SUMMARY_MESSAGE_PREFIX.strip()
        ):
            return True
    return False


def _mark_hierarchy_injected(session_id: str, memory_context: str) -> None:
    """功能：hierarchy 注入后把其包含的 project entry id 标记为本轮已注入——
    memory_source 检索同 entry 时直接剔除（A 通道与 B/C 通道 IDS 去重）。"""
    if not session_id or not memory_context:
        return
    from bandi.context.sources.memory_source import (
        _HIERARCHY_LOCK,
        HIERARCHY_INJECTED_BY_SESSION,
        _memory_id,
        _trim_hierarchy_injected,
    )

    ids = _ENTRY_REF_RE.findall(memory_context)
    if not ids:
        return
    with _HIERARCHY_LOCK:
        bucket = HIERARCHY_INJECTED_BY_SESSION.setdefault(session_id, set())
        for mid in ids:
            bucket.add(_memory_id("project_md", f"entry:{mid}"))
        _trim_hierarchy_injected()


@dataclass
class ContextBundle:
    history: list[Message] = field(default_factory=list)
    referenced_entities: list[Any] = field(default_factory=list)
    estimated_tokens: int = 0
    project_instructions: str = ""
    # 分离承载——记忆不再追加到 project_instructions 末尾（否则 builder 后缀裁剪
    # 会优先裁掉层级记忆）。builder 按「规则优先 + 记忆语义装箱」分别处理。
    memory_context: str = ""
    ontology_schema: str = ""
    memory_rules_active: bool = False
    # 功能：会话是否归属项目（False = 无项目会话，由秘书身份接管）
    project_scoped: bool = False
    # 功能：本会话真实工作区路径（员工工作区/项目根）——系统提示据此列可访问路径
    workspace_paths: list[str] = field(default_factory=list)


class ContextLoader(RuntimeContextSource):
    def __init__(
        self,
        session_store: SessionStore,
        entity_lookup: Any | None = None,
        project_root: str | None = None,
        hierarchy: Any | None = None,
        project_id: str | None = None,
        employee_id: str | None = None,
        team_id: str | None = None,
        group_conversation_id: str = "",
        dm_conversation_id: str = "",
        ontology_store: Any | None = None,
    ) -> None:
        self._session_store = session_store
        self._entity_lookup = entity_lookup
        self._project_instructions_root = project_root
        self._project_instructions = self._load_project_instructions(project_root)
        self._ontology_store = ontology_store
        self._hierarchy = hierarchy
        self._project_id = project_id
        self._employee_id = employee_id
        self._team_id = team_id
        self._group_conversation_id = group_conversation_id
        self._dm_conversation_id = dm_conversation_id

    @property
    def _ontology_schema(self) -> str:
        """功能：词表段的惰性只读视图（每次访问重渲）。

        保留为属性而非构造期字段：词表会随工具写入变化，构造期渲一次会让
        提示词与实际词表长期不一致——模型按旧词汇提交新断言被拒时无从判断。
        """
        return self._render_ontology_schema()

    def _render_ontology_schema(self) -> str:
        """功能：渲染本体词表段——有三元组存储时注入词表，否则为空段。

        每次装载都重渲（不缓存到构造期）：本图会被工具写入，构造期渲一次会让
        提示词与实际词表长期不一致——模型看到旧词汇却提交新断言，被拒时无从判断。
        词表 ≤60 词，重渲代价可忽略。

        三元组本体缺失或渲染异常时静默退化为空段：本体是增强，不是主链依赖。
        """
        if self._ontology_store is not None:
            try:
                from bandi.ontology.prompt import render_ontology_section

                return render_ontology_section(self._ontology_store.graph())
            except Exception as e:  # noqa: BLE001 — 词表渲染失败不得阻断上下文组装
                logger.warning("ontology prompt render failed, falling back: %s", e)
        return ""

    @staticmethod
    def _restore_message(entry: dict) -> Message:
        """
        （共享实现见 core.message.message_from_entry）。"""
        from bandi.core.message import message_from_entry

        return message_from_entry(entry)

    @staticmethod
    def _read_home_file(filename: str) -> str:
        """功能：读取 ~/.bandi/<filename>（文件缓存）——user 层文件（全局
        AGENTS.md / preferences.md）共用；惰性 Path.home（测试隔离）；
        不存在/空返回空串。"""
        path = Path.home() / ".bandi" / filename
        if not path.is_file():
            return ""
        from bandi.perception.project_file_cache import get_project_file_cache

        text = get_project_file_cache().read_text(path)
        return text.strip() if text else ""

    @staticmethod
    def _load_project_instructions(project_root: str | None) -> str:
        if not project_root:
            return ""
        from bandi.perception.project_file_cache import get_project_file_cache

        root = Path(project_root)
        cache = get_project_file_cache()
        parts: list[str] = []
        for fname in _PROJECT_INSTRUCTION_FILES:
            fp = root / fname
            if fp.is_file():
                text = cache.read_text(fp)
                if text:
                    parts.append(f"# {fname}\n\n{text.strip()}")
        memory_md = root / ".bandi" / "memory" / "MEMORY.md"
        if memory_md.is_file():
            raw = cache.read_text(memory_md)
            if raw and raw.strip():
                # load_visible_text 为纯函数（读文件→strip_structured_block）
                # 缓存原始文本后直调静态 strip，避免 store 内部二次读盘
                from bandi.memory.markdown_memory import MarkdownMemoryStore

                memory_text = MarkdownMemoryStore.strip_structured_block(raw)
                if memory_text.strip():
                    parts.append(f"# 项目记忆\n\n{memory_text}")
        return "\n\n".join(parts)

    def get_project_instructions(self) -> str:
        return self._project_instructions

    @staticmethod
    def _project_root_from_session(session: dict) -> str | None:
        raw_metadata = session.get("metadata") or "{}"
        if isinstance(raw_metadata, dict):
            root = raw_metadata.get("project_root")
        else:
            import json

            try:
                root = json.loads(raw_metadata).get("project_root")
            except (ValueError, TypeError):
                return None
        return str(root) if root else None

    async def load(
        self, message: Message, session_id: str, max_tokens: int = 4096
    ) -> ContextBundle:
        history: list[Message] = []
        if session_id:
            session = self._session_store.get_session(session_id)
            if session:
                entries = self._session_store.get_branch(session_id)
                history = [
                    self._restore_message(e) for e in entries if e.get("entry_type") == "message"
                ]
        entities: list[Any] = []
        if self._entity_lookup and message.content:
            refs = self._extract_entity_refs(message.content)
            for ref in refs:
                entity = self._entity_lookup.get_entity(ref)
                if entity is None:
                    # 名字引用回退：按名称模糊匹配（@恒星贸易项目 → find_entities）
                    found = self._entity_lookup.find_entities(query=ref, limit=1)
                    entity = found[0] if found else None
                if entity:
                    entities.append(entity)
        from bandi.utils.token_counter import TokenCounter

        total_est = TokenCounter.estimate(
            " ".join(h.content or "" for h in history)
        ) + TokenCounter.estimate(message.content or "")

        project_instructions = self._project_instructions
        memory_rules_active = self._project_instructions_root is not None
        if session_id:
            session = self._session_store.get_session(session_id)
            if session:
                session_root = self._project_root_from_session(session)
                if session_root is not None and session_root != self._project_instructions_root:
                    project_instructions = self._load_project_instructions(session_root)
                    memory_rules_active = True
        # → 用户偏好），项目在后＝更具体层覆盖；每轮读取（文件缓存）
        user_prefix_parts: list[str] = []
        global_instructions = self._read_home_file("AGENTS.md")
        if global_instructions:
            user_prefix_parts.append(f"# 全局 AGENTS.md\n\n{global_instructions}")
        preferences = self._read_home_file("preferences.md")
        if preferences:
            user_prefix_parts.append(
                "## 用户偏好（直接将这些偏好应用于与用户的交互——如称呼方式；"
                "不要在回复中复述偏好条目本身；可追加更新：~/.bandi/preferences.md）\n\n"
                f"{preferences}"
            )
        if user_prefix_parts:
            prefix = "\n\n".join(user_prefix_parts)
            project_instructions = (
                f"{prefix}\n\n{project_instructions}" if project_instructions else prefix
            )
        memory_context = ""
        if self._hierarchy is not None and (
            self._group_conversation_id or self._dm_conversation_id or self._project_id
        ):
            # 功能：builder 后缀裁剪后先取到高优先级记忆
            memory_context = self._hierarchy.collect_memory_context(
                project_id=self._project_id or "",
                session_id=session_id or "",
                employee_id=self._employee_id or "",
                group_conversation_id=self._group_conversation_id,
                dm_conversation_id=self._dm_conversation_id,
                max_tokens=max_tokens,
                team_id=self._team_id,
            )
            # （无摘要（被二次压缩裁掉）时保留：跨轮唯一找回途径）
            if memory_context and _history_has_compaction_summary(history):
                memory_context = _strip_compaction_notes(memory_context)
            # memory_source/prefetch 检索同 entry 时降权，消除重复注入
            _mark_hierarchy_injected(session_id or "", memory_context)
        total_est += TokenCounter.estimate(project_instructions)
        total_est += TokenCounter.estimate(memory_context)

        workspace_paths: list[str] = []
        if self._employee_id:
            from bandi.tools.fs_tool import employee_workspace_root

            employee_root = employee_workspace_root(
                self._project_instructions_root, self._project_id or "", self._employee_id
            )
            if employee_root is not None:
                workspace_paths.append(str(employee_root))
        if self._project_instructions_root:
            workspace_paths.append(str(self._project_instructions_root))

        return ContextBundle(
            history=history,
            referenced_entities=entities,
            estimated_tokens=total_est,
            project_instructions=project_instructions,
            memory_context=memory_context,
            ontology_schema=self._render_ontology_schema(),
            memory_rules_active=memory_rules_active,
            project_scoped=bool(self._project_id),
            workspace_paths=workspace_paths,
        )

    @staticmethod
    def _extract_entity_refs(content: str) -> list[str]:
        return re.findall(r"@([\w-]+)", content)
