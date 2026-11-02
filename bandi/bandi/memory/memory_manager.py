# 功能：四层记忆层级管理器——global/project/session/employee 快照门面

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS, GlobalMemoryStore
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.session.store import SessionStore

if TYPE_CHECKING:
    from bandi.company.store import WorkItemStore

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"
SCOPE_SESSION = "session"
SCOPE_EMPLOYEE = "employee"

_SCOPES = {SCOPE_GLOBAL, SCOPE_PROJECT, SCOPE_SESSION, SCOPE_EMPLOYEE}

_SCOPE_VALUE = {SCOPE_EMPLOYEE: 4, SCOPE_SESSION: 3, SCOPE_PROJECT: 2, SCOPE_GLOBAL: 1}
_SCOPE_HEADERS = {
    SCOPE_GLOBAL: "## 全局记忆",
    SCOPE_PROJECT: "## 项目记忆",
    SCOPE_SESSION: "## 会话记忆",
    SCOPE_EMPLOYEE: "## 员工记忆",
}

_MAX_PROJECT_SEGMENT = 64


@dataclass
class MemoryBlock:
    """字段化记忆块——priority packing 的原子单元。

    priority = scope 优先级（employee>session>project>global）+ 重要性加权；
    dependencies 引用其他块文本前缀（依赖未装则本块不装）；token_cost 为
    预算装入的精确消耗。
    """

    text: str
    priority: float
    source: str
    importance: float = 0.5
    recency: float = 0.5
    confidence: float = 1.0
    token_cost: int = 0
    dependencies: tuple[str, ...] = ()


# Windows 保留名（mkdir 抛 OSError）+ 尾随点/空格（Windows
# 规范化碰撞）不得作目录段
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _safe_project_segment(project_id: str) -> str | None:
    """

    深度复审修复：拒绝 Windows 保留名（CON/NUL/PRN/AUX/COM1-9/LPT1-9）与
    尾随点/空格（mkdir OSError / 规范化碰撞）。

    批3-5：段统一小写 + 大小写变化追加稳定短哈希——Windows NTFS 大小写不敏感
    `ProjA`/`proja` 靠大小写区分会归一化到同一物理目录 → 跨项目记忆串写；
    统一小写后哈希不同保证目录隔离。

    返回 None 表示不安全（回退默认 store），否则返回可作单目录段的字符串。
    """
    if not project_id or project_id.startswith("."):
        return None
    if "/" in project_id or "\\" in project_id:
        return None
    if project_id == ".." or ".." in project_id.split("."):
        return None
    if project_id.endswith((".", " ")):
        return None
    base = project_id.split(".")[0].lower()
    if base in _WINDOWS_RESERVED_NAMES:
        return None
    segment = re.sub(r"[^A-Za-z0-9_-]", "_", project_id).lower()
    segment = segment[:_MAX_PROJECT_SEGMENT]
    # 段碰撞防串写——`proj..x` 与 `proj__x`、64 截断前缀相同、大小写变化
    # 的 project_id 会派生同段 → 跨项目记忆串写。追加稳定短哈希区分
    # （仅当替换/截断/大小写变化发生时才追加，安全 ID 直用不哈希）
    if segment != project_id:
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:8]
        segment = f"{segment[: _MAX_PROJECT_SEGMENT - 9]}_{digest}"
    return segment


_ENTRY_HEAD_MARKER = "["


def _encoded_global_len(text: str) -> int:
    """功能：全局层存储编码长度（`json.dumps(ensure_ascii=False)` 后字符数）。"""
    return len(json.dumps(text, ensure_ascii=False))


def _min_index_within_capacity(text: str, limit: int = GLOBAL_MEMORY_MAX_CHARS) -> int:
    """功能：二分定位最小原始索引 i，使 `text[i]` 编码后不超容量。

    编码长度差不是原始字符偏移：`"`/`\\`/换行转义后 1 字符变 2，偏移可能大于
    全文长度 → 直接切片会得到空串（清空整块全局记忆）。
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if _encoded_global_len(text[mid:]) > limit:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _trim_global_merged(text: str) -> str:
    """功能：全局共享层超容量保尾裁剪——按 JSON 编码长度、沿完整条目边界丢最旧。

    1. 编码未超容量直接返回；
    2. 二分定位满足编码上限的最小原始索引（与转义开销无关，杜绝高转义清空）；
    3. 对齐行边界后跳过「半截条目」残行，保留头从完整条目头（`[` 标记）开始；
       无条目标记或无行边界的长单行回退编码边界，宁多丢最旧也不丢最新。
    """
    if _encoded_global_len(text) <= GLOBAL_MEMORY_MAX_CHARS:
        return text
    start = _min_index_within_capacity(text)
    boundary = text.find("\n", start)
    if boundary == -1 or boundary + 1 >= len(text):
        # 无行边界 / 边界即文末（结尾换行）——回退编码边界，绝不清空最新内容
        return text[start:]
    index = boundary + 1
    head = index
    while head < len(text) and text[head] != _ENTRY_HEAD_MARKER:
        next_boundary = text.find("\n", head)
        if next_boundary == -1:
            return text[index:]
        head = next_boundary + 1
        return text[head:] if head < len(text) else text[index:]


_LESSON_TEAM_RE = re.compile(r"^\[教训\|team=([^\]]+)\]")


def _filter_global_lessons_by_team(text: str, team_id: str | None) -> str:
    """功能：全局教训按团队行级过滤——保留无标签通用条目与本团队条目；
    无团队上下文（team_id 空）仅保留无标签条目（安全默认，防跨团队泄漏）；
    非教训行一律保留（兼容未来写入，避免误删全局层其他内容）。
    """
    current = (team_id or "").strip()
    kept: list[str] = []
    for line in text.splitlines():
        match = _LESSON_TEAM_RE.match(line.strip())
        if match is None:
            kept.append(line)
        elif current and match.group(1) == current:
            kept.append(line)
    return "\n".join(kept)


class MemoryHierarchyManager:
    def __init__(
        self,
        global_store: GlobalMemoryStore,
        markdown_memory: MarkdownMemoryStore,
        work_item_store: WorkItemStore,
        session_store: SessionStore | None = None,
        project_stores: dict[str, MarkdownMemoryStore] | None = None,
        store_dir: str | Path | None = None,
    ) -> None:
        self._global = global_store
        self._markdown = markdown_memory
        self._work_items = work_item_store
        self._session_store = session_store
        self._project_stores = project_stores or {}
        self._store_dir = Path(store_dir) if store_dir is not None else None
        self._project_store_cache: dict[str, MarkdownMemoryStore] = {}

    def _project_store(self, project_id: str) -> MarkdownMemoryStore:
        store = self._project_stores.get(project_id)
        if store is not None:
            return store
        # 中文/不安全 ID 使用安全派生目录段，避免跨项目串写
        segment = _safe_project_segment(project_id) if self._store_dir is not None else None
        if segment is None:
            return self._markdown
        store = self._project_store_cache.get(segment)
        if store is None:
            project_root = self._store_dir / "projects" / segment
            store = MarkdownMemoryStore(project_root, project_root=project_root)
            self._project_store_cache[segment] = store
        return store

    def save_memory(
        self, scope: str, content: str, *, allow_shared: bool = False, **identifiers: Any
    ) -> None:
        self._validate_scope(scope)
        if scope == SCOPE_GLOBAL:
            # 🟢-G：空/纯空白内容无信息量——写入只会落成一个孤零零的分隔换行
            if not content or not content.strip():
                return
            if not allow_shared:
                # 共享白板写入必须显式声明——默认降级到调用方自己的命名空间
                self._write_global_fallback(content, identifiers)
                return
            # 🔴-2 修复：全局共享层写入改为**合并**，不再 `put_global_memory` 整键
            # 每写一条就把整块全局记忆覆盖掉（顺序写也丢数据，不只是并发丢更新）。
            # 与 append 共用同一「写锁 + 版本 CAS」路径，语义归一；同内容幂等
            # （同 turn checkpoint 重放不重复堆叠）。
            if content and content in self.load_memory(scope):
                return
            self._merge_global_shared(content)
        elif scope == SCOPE_PROJECT:
            project_id = self._require(identifiers, "project_id")
            self._project_store(project_id).save_visible_text(content)
        elif scope == SCOPE_SESSION:
            self._save_session_snapshot(content, identifiers, memory_kind="process")
        elif scope == SCOPE_EMPLOYEE:
            self._save_session_snapshot(content, identifiers, memory_kind="final")

    def _merge_global_shared(self, entry: str) -> None:
        """功能：全局共享层读-改-写合并（store 级写锁 + 版本 CAS + 退避重试）。

        调用方必须已完成 allow_shared 判定。超容量时保留最新（按行边界丢最旧）。
        """
        with self._global.write_lock:
            for attempt in range(_GLOBAL_CAS_ATTEMPTS):
                expected = self._global.get_global_memory_version(_GLOBAL_KEY)
                current = self.load_memory(SCOPE_GLOBAL)
                merged = f"{current}\n{entry}" if current else entry
                # 4.12 + 🔴-A：超容量保留最新（丢最旧、按行边界切）——改用
                # **JSON 编码后长度** 裁剪，从源头保证写入不触发下游
                # `_prepare_global_value` 的保头二次截断（原按原始字符数裁剪
                # 换行转义后仍超限 → 最新写入被砍 + 结尾碎片）。
                merged = _trim_global_merged(merged)
                try:
                    if self._global.put_global_memory_cas(_GLOBAL_KEY, merged, expected):
                        return
                except sqlite3.OperationalError:
                    # 跨进程写锁竞争（BEGIN IMMEDIATE 等锁超时）时退避后重读版本再试；
                    # 末次仍失败则原样抛出，不把数据库错误伪装成 CAS 争用
                    if attempt == _GLOBAL_CAS_ATTEMPTS - 1:
                        raise
                if attempt < _GLOBAL_CAS_ATTEMPTS - 1:
                    time.sleep(_GLOBAL_CAS_BACKOFF_SEC * (attempt + 1))
            raise RuntimeError(
                f"global memory CAS write failed after {_GLOBAL_CAS_ATTEMPTS} attempts "
                "(write contention)"
            )

    def load_memory(self, scope: str, **identifiers: Any) -> str:
        self._validate_scope(scope)
        if scope == SCOPE_GLOBAL:
            value = self._global.get(_GLOBAL_KEY)
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            # 非字符串值按存储格式（JSON）还原，不得落 Python repr
            return json.dumps(value, ensure_ascii=False)
        if scope == SCOPE_PROJECT:
            project_id = self._require(identifiers, "project_id")
            return self._project_store(project_id).load_visible_text()
        if scope == SCOPE_SESSION:
            if not identifiers.get("session_id"):
                return ""
            return self._load_snapshot_text(identifiers, memory_kind="process")
        return self._load_snapshot_text(identifiers, memory_kind="final")

    def append_memory(
        self, scope: str, entry: str, *, allow_shared: bool = False, **identifiers: Any
    ) -> None:
        self._validate_scope(scope)
        if scope == SCOPE_GLOBAL:
            if not allow_shared:
                self._append_global_fallback(entry, identifiers)
                return
            # load→merge→save 无锁无 CAS，并发追加丢更新（逻辑与 save 共用）
            # 🟢-F：同 entry 重放幂等（与 SESSION/EMPLOYEE 分支同口径）
            # 同 turn checkpoint 重放不重复堆叠
            if entry and entry in self.load_memory(scope):
                return
            self._merge_global_shared(entry)
        elif scope == SCOPE_PROJECT:
            project_id = self._require(identifiers, "project_id")
            self._project_store(project_id).append_visible_entry(entry)
        elif scope in (SCOPE_SESSION, SCOPE_EMPLOYEE):
            # 快照覆盖式 save 不冲纪要，跨轮存活）；幂等（同 entry 跳过）由 store
            # 单事务保证（原读-改-写 save 会把纪要写进 memory_text 被快照覆盖）
            employee_id = self._require(identifiers, "employee_id")
            session_id = str(identifiers.get("session_id", "") or "")
            project_id = (
                str(identifiers.get("project_id") or "").strip() or _DEFAULT_PROJECT_NAMESPACE
            )
            memory_scope = SCOPE_SESSION if session_id else SCOPE_EMPLOYEE
            memory_kind = "process" if memory_scope == SCOPE_SESSION else "final"
            self._work_items.append_snapshot_summary(
                project_id=project_id,
                session_id=session_id,
                employee_id=employee_id,
                memory_scope=memory_scope,
                memory_kind=memory_kind,
                entry=entry,
            )

    def _write_global_fallback(self, content: str, identifiers: dict[str, Any]) -> None:
        """功能：——未显式共享的「全局」写入降级到调用方自己的命名空间。

        有 employee_id → 员工层；仅有 project_id → 项目层；两者都无 → 显式拒绝
        （不再静默写所有人的共享白板）。
        """
        project_id = identifiers.get("project_id")
        employee_id = identifiers.get("employee_id")
        if employee_id:
            logger.warning(
                "global memory write downgraded to employee namespace (employee=%s)", employee_id
            )
            self._save_session_snapshot(content, identifiers, memory_kind="final")
            return
        if project_id:
            logger.warning(
                "global memory write downgraded to project namespace (project=%s)", project_id
            )
            self._project_store(str(project_id)).save_visible_text(content)
            return
        raise ValueError(
            "global memory write requires allow_shared=True or project_id/employee_id "
            "(refusing silent shared-board write)"
        )

    def _append_global_fallback(self, entry: str, identifiers: dict[str, Any]) -> None:
        """功能：——未显式共享的「全局」追加降级到调用方自己的命名空间（幂等）。"""
        project_id = identifiers.get("project_id")
        employee_id = identifiers.get("employee_id")
        if employee_id:
            logger.warning(
                "global memory append downgraded to employee namespace (employee=%s)", employee_id
            )
            current = self.load_memory(SCOPE_EMPLOYEE, **identifiers) or ""
            if entry in current:
                return
            merged = f"{current}\n{entry}" if current else entry
            self._save_session_snapshot(merged, identifiers, memory_kind="final")
            return
        if project_id:
            logger.warning(
                "global memory append downgraded to project namespace (project=%s)", project_id
            )
            self._project_store(str(project_id)).append_visible_entry(entry)
            return
        raise ValueError(
            "global memory append requires allow_shared=True or project_id/employee_id "
            "(refusing silent shared-board write)"
        )

    def append_event(
        self, event_type: str, payload: dict[str, Any] | None = None, *, event_id: str | None = None
    ) -> int:
        return self._global.append_event(event_type, payload, event_id=event_id)

    def query_events(self, event_type: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._global.query_events(event_type, limit)

    def collect_memory_context(
        self,
        project_id: str,
        session_id: str,
        employee_id: str,
        max_tokens: int = 4096,
        team_id: str | None = None,
        group_conversation_id: str = "",
        dm_conversation_id: str = "",
    ) -> str:
        group_text = ""
        dm_text = ""
        group_header = _SCOPE_HEADERS[SCOPE_PROJECT]
        memory_session_id = session_id
        base_project_id = project_id
        if dm_conversation_id:
            from bandi.memory.dm_scope import DM_SNAPSHOT_EMPLOYEE_ID, dm_scope_id

            project_id = dm_scope_id(base_project_id, dm_conversation_id)
            session_id = ""
            employee_id = ""
            group_header = "## 员工单聊记忆"
            snapshot = self._work_items.get_memory_snapshot(
                project_id=project_id,
                session_id=memory_session_id,
                employee_id=DM_SNAPSHOT_EMPLOYEE_ID,
                memory_scope="dm",
            )
            if snapshot is not None:
                dm_text = str(snapshot.get("memory_text") or "")
            else:
                dm_text = self.load_memory(SCOPE_PROJECT, project_id=project_id)
                if dm_text:
                    self._work_items.save_memory_snapshot(
                        project_id=project_id,
                        session_id=memory_session_id,
                        employee_id=DM_SNAPSHOT_EMPLOYEE_ID,
                        role_id="",
                        memory_scope="dm",
                        memory_kind="final",
                        memory_text=dm_text,
                    )
        if group_conversation_id:
            from bandi.memory.group_scope import (
                GROUP_SNAPSHOT_EMPLOYEE_ID,
                group_scope_id,
            )

            project_id = group_scope_id(group_conversation_id)
            session_id = ""
            employee_id = ""
            group_header = "## 群聊记忆"
            snapshot = self._work_items.get_memory_snapshot(
                project_id=project_id,
                session_id=memory_session_id,
                employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
                memory_scope="group",
            )
            if snapshot is not None:
                group_text = str(snapshot.get("memory_text") or "")
            else:
                group_text = self.load_memory(SCOPE_PROJECT, project_id=project_id)
                if group_text:
                    self._work_items.save_memory_snapshot(
                        project_id=project_id,
                        session_id=memory_session_id,
                        employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
                        role_id="",
                        memory_scope="group",
                        memory_kind="final",
                        memory_text=group_text,
                    )
        sections: list[tuple[str, str, str]] = []
        global_text = self.load_memory(SCOPE_GLOBAL)
        if global_text:
            # 共库不得跨团队混合；无团队上下文仅保留无标签通用条目
            global_text = _filter_global_lessons_by_team(global_text, team_id)
        if global_text:
            sections.append((SCOPE_GLOBAL, _SCOPE_HEADERS[SCOPE_GLOBAL], global_text))
        project_text = (
            group_text
            if group_conversation_id
            else dm_text
            if dm_conversation_id
            else self.load_memory(SCOPE_PROJECT, project_id=project_id)
        )
        if project_text:
            sections.append((SCOPE_PROJECT, group_header, project_text))
        if session_id:
            session_text = self.load_memory(
                SCOPE_SESSION, project_id=project_id, session_id=session_id, employee_id=employee_id
            )
            if session_text:
                sections.append((SCOPE_SESSION, _SCOPE_HEADERS[SCOPE_SESSION], session_text))
        if not group_conversation_id and not dm_conversation_id:
            employee_text = self.load_memory(
                SCOPE_EMPLOYEE, project_id=project_id, employee_id=employee_id
            )
            if employee_text:
                sections.append((SCOPE_EMPLOYEE, _SCOPE_HEADERS[SCOPE_EMPLOYEE], employee_text))
        if not sections:
            return ""
        # 快照 ⇒ 两段注入同事实（白烧 token、可能被模型当强调）。逐行去重：
        # 保留优先级更高的前段（global→project→session→employee），空行不参与。
        seen_lines: set[str] = set()
        deduped: list[tuple[str, str, str]] = []
        for scope, header, text in sections:
            kept: list[str] = []
            for line in text.splitlines():
                key = line.strip()
                if key:
                    if key in seen_lines:
                        continue
                    seen_lines.add(key)
                kept.append(line)
            if any(part.strip() for part in kept):
                deduped.append((scope, header, "\n".join(kept)))
        sections = deduped
        if not sections:
            return ""
        return self._apply_block_budget(sections, max_tokens)

    @classmethod
    def _blockize(
        cls,
        text: str,
        source: str,
        scope_priority: float,
        *,
        importance: float = 0.5,
        recency: float = 0.5,
    ) -> list[MemoryBlock]:
        """功能：文本切块 + 字段化——priority = scope 优先级 + 重要性/新近度加权
        （文本块无结构化元数据，中性值由调用方覆盖）。"""
        from bandi.utils.token_counter import TokenCounter

        blocks: list[MemoryBlock] = []
        for blk in cls._split_blocks(text):
            blocks.append(
                MemoryBlock(
                    text=blk,
                    priority=scope_priority + importance * 0.1 + recency * 0.05,
                    source=source,
                    importance=importance,
                    recency=recency,
                    confidence=1.0,
                    token_cost=max(1, TokenCounter.estimate(blk)),
                )
            )
        return blocks

    @classmethod
    def _pack_blocks(cls, blocks: list[MemoryBlock], max_tokens: int) -> list[MemoryBlock]:
        """功能：字段化块级 priority packing——priority 降序贪心 + 依赖约束
        （依赖未装入不装本块，迭代直到无进展）+ 必留首行兜底。"""
        if max_tokens <= 0 or not blocks:
            return []
        from bandi.utils.token_counter import TokenCounter

        remaining = max_tokens
        installed: list[MemoryBlock] = []
        installed_keys: set[str] = set()
        pending = sorted(blocks, key=lambda b: (-b.priority, b.token_cost))
        progress = True
        while pending and progress:
            progress = False
            for blk in list(pending):
                if blk.dependencies and not all(d in installed_keys for d in blk.dependencies):
                    continue
                pending.remove(blk)
                progress = True
                if blk.token_cost <= remaining:
                    installed.append(blk)
                    installed_keys.add(blk.text[:80])
                    remaining -= blk.token_cost
                    continue
                prefix = cls._mandatory_prefix(blk.text)
                if prefix:
                    prefix_cost = max(1, TokenCounter.estimate(prefix))
                    if prefix_cost <= remaining:
                        installed.append(
                            MemoryBlock(
                                text=prefix,
                                priority=blk.priority,
                                source=blk.source,
                                importance=blk.importance,
                                recency=blk.recency,
                                confidence=blk.confidence,
                                token_cost=prefix_cost,
                            )
                        )
                        remaining -= prefix_cost
        return installed

    @classmethod
    def _apply_block_budget(cls, sections: list[tuple[str, str, str]], max_tokens: int) -> str:
        """MemoryBlock 字段化 packing——priority 降序贪心（scope 优先级 +
        重要性/新近度加权），依赖约束，必留首行兜底；输出按 sections 展示顺序。"""
        blocks: list[MemoryBlock] = []
        for scope, _header, body in sections:
            blocks.extend(cls._blockize(body, scope, float(_SCOPE_VALUE[scope])))
        packed = cls._pack_blocks(blocks, max_tokens)
        out: list[str] = []
        for scope, header, _body in sections:
            kept = [b.text for b in packed if b.source == scope]
            if kept:
                out.append(f"{header}\n" + "\n\n".join(kept))
        return "\n\n".join(out)

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        """按空行段落切语义块——项目多条目/会话多轮各自独立，背包可只取能放的块。"""
        parts = [p for p in (text or "").split("\n\n") if p.strip()]
        return parts or ([text.strip()] if (text or "").strip() else [])

    @classmethod
    def _mandatory_prefix(cls, block: str) -> str:
        """字段级必留（审查项7）：块整体放不进预算时，其**必留标识字段**＝首行
        （一般承载块语义身份：条目标题/决策标签/关键标记）。返回首行；单行块无
        可省体（首行即整块）→ 返回空表示不可拆，整块舍弃省得产生无身份残片。"""
        lines = block.splitlines()
        if len(lines) <= 1:
            return ""
        first = lines[0].strip()
        return first if first else ""

    def _save_session_snapshot(
        self,
        content: str,
        identifiers: dict[str, Any],
        *,
        memory_kind: str,
    ) -> None:
        employee_id = self._require(identifiers, "employee_id")
        session_id = str(identifiers.get("session_id", "") or "")
        # project_id，与 `_write_global_fallback`「有 employee_id → 员工层」的
        # 承诺矛盾；本地单机形态下归入默认项目命名空间。
        project_id = str(identifiers.get("project_id") or "").strip() or _DEFAULT_PROJECT_NAMESPACE
        memory_scope = SCOPE_SESSION if session_id else SCOPE_EMPLOYEE
        if memory_scope == SCOPE_SESSION and self._session_store is not None:
            self._ensure_session_record(session_id, project_id, employee_id, memory_kind)
        self._work_items.save_memory_snapshot(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
            role_id=str(identifiers.get("role_id", "") or ""),
            memory_scope=memory_scope,
            memory_kind=memory_kind,
            memory_text=content,
        )

    def _ensure_session_record(
        self,
        session_id: str,
        project_id: str,
        employee_id: str,
        memory_kind: str,
    ) -> None:
        existing = self._session_store.get_session(session_id)
        if existing is None:
            return
        metadata = json.loads(existing.get("metadata") or "{}")
        metadata.update(
            {
                "project_id": project_id,
                "employee_id": employee_id,
                "memory_kind": memory_kind,
            }
        )
        self._session_store.update_session(
            session_id, metadata=json.dumps(metadata, ensure_ascii=False)
        )

    def _load_snapshot_text(
        self,
        identifiers: dict[str, Any],
        *,
        memory_kind: str,
    ) -> str:
        employee_id = self._require(identifiers, "employee_id")
        session_id = str(identifiers.get("session_id", "") or "")
        # 否则「写得进、读不出」
        project_id = str(identifiers.get("project_id") or "").strip() or _DEFAULT_PROJECT_NAMESPACE
        memory_scope = SCOPE_SESSION if session_id else SCOPE_EMPLOYEE
        snapshot = self._work_items.get_memory_snapshot(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
            memory_scope=memory_scope,
            memory_kind=memory_kind,
        )
        if snapshot is None:
            return ""
        # （summary_text，追加式）——纪要字段分离后必须可读回（压缩纪要契约
        # 「本轮快照 + 压缩纪要共存」不变式）
        body = str(snapshot.get("memory_text") or "")
        summary = str(snapshot.get("summary_text") or "")
        if summary:
            return f"{body}\n{summary}" if body else summary
        return body

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in _SCOPES:
            raise ValueError(f"未知记忆层级 scope={scope!r}，可选 {sorted(_SCOPES)}")

    def close(self) -> None:
        # 项目缓存）。session_store 为外部传入（所有权归调用方）不在此关闭
        # 避免与调用方重复关闭。
        closers: list[Any] = [self._global, self._work_items, self._markdown]
        closers.extend(self._project_stores.values())
        closers.extend(self._project_store_cache.values())
        for closer in closers:
            close_fn = getattr(closer, "close", None)
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 — 关闭失败不阻断后续清理
                    pass

    @staticmethod
    def _require(identifiers: dict[str, Any], key: str) -> str:
        value = identifiers.get(key)
        if not value:
            raise ValueError(f"scope 需要参数 {key!r}")
        return str(value)


_GLOBAL_CAS_ATTEMPTS = 3
_GLOBAL_CAS_BACKOFF_SEC = 0.05
_DEFAULT_PROJECT_NAMESPACE = "default"
_GLOBAL_KEY = "global_hierarchy_memory"
