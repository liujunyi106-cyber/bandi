# 功能：群聊记忆清理——按会话记忆域删除经历/长期/入口文件/快照/待审批项
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bandi.company.store import WorkItemStore
from bandi.memory.episodic import EpisodicStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.group_scope import group_scope_id
from bandi.memory.long_term import LongTermMemory
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager

logger = logging.getLogger(__name__)


def _default_data_dir() -> Path:
    return Path.home() / ".bandi" / "data"


def _discard_pending(project_id: str, store: Any) -> int:
    discarded = 0
    for record in store.list_pending():
        payload = getattr(record, "payload", None) or {}
        if str(payload.get("project_id") or "") != project_id:
            continue
        if store.discard_pending(record.id8):
            discarded += 1
    return discarded


def purge_group_memory(
    conversation_id: str,
    *,
    data_dir: Path | None = None,
    pending_store: Any | None = None,
) -> dict[str, Any]:
    """功能：删除一个群聊会话的全部群记忆，不触碰团队/全局/其他会话记忆。"""
    scope = group_scope_id(conversation_id)
    result: dict[str, Any] = {
        "episodes": 0,
        "long_term": 0,
        "project_md": False,
        "snapshots": 0,
        "pending": 0,
        "ok": True,
    }
    if not scope:
        return result

    base = Path(data_dir) if data_dir is not None else _default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    episodic = EpisodicStore(str(base / "episodic.db"))
    long_term = LongTermMemory(str(base / "long_term.db"))
    work_items = WorkItemStore(str(base / "hierarchy_work_items.db"))
    hierarchy = MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(base / "hierarchy_global.db")),
        markdown_memory=MarkdownMemoryStore(base),
        work_item_store=work_items,
        store_dir=base,
    )
    try:
        result["episodes"] = episodic.delete_scope(scope, employee_id=None, team_id=None)
        result["long_term"] = long_term.delete_scope(scope, employee_id=None)
        result["project_md"] = hierarchy.purge_project_memory(scope)
        result["snapshots"] = work_items.delete_memory_snapshots_by_project(scope)
        if pending_store is None:
            from bandi.memory.staging import get_pending_store

            pending_store = get_pending_store()
        result["pending"] = _discard_pending(scope, pending_store)
        from bandi.context.sources.memory_source import forget_session

        forget_session(str(conversation_id))
    except Exception as exc:  # noqa: BLE001 — 删除尽力而为，失败由调用方提示重试
        result["ok"] = False
        logger.warning("group_memory_purge_failed [%s]: %s", scope, exc)
    finally:
        for closer in (hierarchy, episodic, long_term):
            try:
                closer.close()
            except Exception:  # noqa: BLE001 — 关闭失败不阻断清理结果
                pass
    return result
