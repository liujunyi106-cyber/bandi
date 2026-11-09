# 功能：经理工具面——board 巡检（manager_board_read）+ 子卡修订（modify_work_item）+
# 子卡取消（delete_work_item）。所有权 fail-closed（仅父卡负责人可操作），
# 变更记账写入父卡 metadata 供 dispatch guard 消费。

from __future__ import annotations

from typing import Any

from bandi.company.phase import Phase
from bandi.company.work_item import WorkItem, WorkItemTurnType, transition_work_item
from bandi.tools.base import (
    BaseTool,
    RetryPolicy,
    ToolCategory,
    ToolPermissionContext,
)

_DEPENDENCY_IDS_KEY = "dependency_work_item_ids"
_MUTATION_FLAG_KEY = "manager_board_mutation_performed"
_MUTATION_MODIFIED_KEY = "manager_board_modified_work_item_ids"
_MUTATION_DELETED_KEY = "manager_board_deleted_work_item_ids"

_TERMINAL_PHASES = frozenset({Phase.APPROVED, Phase.FAILED, Phase.CANCELLED})
_REVIEW_PHASES = frozenset({Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN})
_MUTABLE_PHASES = frozenset(
    {
        Phase.QUEUED,
        Phase.READY,
        Phase.WAITING_DEPENDENCIES,
        Phase.READY_FOR_REWORK,
        Phase.PAUSED,
    }
)

_TURN_TYPE_MAP: dict[str, WorkItemTurnType] = {
    "plan": WorkItemTurnType.PLAN,
    "execute": WorkItemTurnType.EXECUTE,
    "followup": WorkItemTurnType.FOLLOWUP,
}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def _caller_role(context: ToolPermissionContext | None) -> str:
    return str(getattr(context, "role", "") or "").strip()


def _owns(parent: WorkItem, caller_role: str) -> bool:
    owner = str(getattr(parent, "assignee_role", "") or "").strip()
    return bool(owner) and bool(caller_role) and owner.lower() == caller_role.lower()


def _child_of(store: Any, work_item_id: str) -> tuple[WorkItem | None, WorkItem | None, str]:
    child = store.get(work_item_id)
    if child is None:
        return None, None, f"work item '{work_item_id}' not found"
    parent_id = str(getattr(child, "parent_id", "") or "").strip()
    if not parent_id:
        return child, None, "only child work items can be managed (no parent)"
    parent = store.get(parent_id)
    if parent is None:
        return child, None, f"parent work item '{parent_id}' not found"
    return child, parent, ""


def _record_parent_mutation(
    store: Any,
    parent: WorkItem,
    *,
    modified_id: str = "",
    deleted_id: str = "",
) -> None:
    # 功能：父卡变更记账（dispatch guard 依据；并发写由工具串行化保证）。
    metadata = dict(parent.metadata or {})
    metadata[_MUTATION_FLAG_KEY] = True
    if modified_id:
        ids = [str(v) for v in (metadata.get(_MUTATION_MODIFIED_KEY) or [])]
        metadata[_MUTATION_MODIFIED_KEY] = list(dict.fromkeys([*ids, modified_id]))
    if deleted_id:
        ids = [str(v) for v in (metadata.get(_MUTATION_DELETED_KEY) or [])]
        metadata[_MUTATION_DELETED_KEY] = list(dict.fromkeys([*ids, deleted_id]))
    parent.metadata = metadata
    store.save(parent)


class ManagerBoardReadTool(BaseTool):
    name: str = "manager_board_read"
    description: str = (
        "Manager board inspection: list the child work items under a parent WorkItem "
        "you own (id/title/assignee/phase/turn_type/thread), to reconcile the existing "
        "board before delegating or modifying. Read-only."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "parent_work_item_id": {
                "type": "string",
                "description": "父 work_item id（须在调用者名下）",
            },
            "include_terminal": {
                "type": "boolean",
                "description": "是否包含已终态子卡（默认 true）",
            },
        },
        "required": ["parent_work_item_id"],
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        self.store = store
        self.org_engine = org_engine

    def bind_deps(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        """后绑定依赖——公司链路装配点注入；None 保持 fail-closed。"""
        if store is not None:
            self.store = store
        if org_engine is not None:
            self.org_engine = org_engine

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        if self.store is None or self.org_engine is None:
            return _err("manager_board_read requires store/org_engine deps")
        parent_id = str(params.get("parent_work_item_id") or "").strip()
        if not parent_id:
            return _err("manager_board_read requires parent_work_item_id")
        parent = self.store.get(parent_id)
        if parent is None:
            return _err(f"parent work item '{parent_id}' not found")
        caller = _caller_role(context)
        if not _owns(parent, caller):
            return _err("caller is not the owner of this board (parent assignee mismatch)")
        include_terminal = params.get("include_terminal", True)
        include_terminal = True if include_terminal is None else bool(include_terminal)
        children: list[dict[str, Any]] = []
        for item in self.store.list_all():
            if str(getattr(item, "parent_id", "") or "") != parent_id:
                continue
            if not include_terminal and item.phase in _TERMINAL_PHASES:
                continue
            turn_type = getattr(item, "turn_type", "")
            children.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "assignee_role": item.assignee_role or "",
                    "phase": item.phase.value,
                    "turn_type": str(getattr(turn_type, "value", turn_type) or ""),
                    "thread_id": str((item.metadata or {}).get("thread_id") or ""),
                    "updated_at": str(getattr(item, "updated_at", "") or ""),
                }
            )
        return {
            "success": True,
            "parent_work_item_id": parent_id,
            "children": children,
        }


class ModifyWorkItemTool(BaseTool):
    name: str = "modify_work_item"
    description: str = (
        "Modify an existing child WorkItem you own before it runs: update title / "
        "description / turn_type (plan|execute|followup). Rejects running, review-pending "
        "and terminal cards. Reassignment is not supported — delete the card and spawn a "
        "replacement instead."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "work_item_id": {"type": "string", "description": "要修订的子卡 id"},
            "title": {"type": "string", "description": "新标题（可选）"},
            "description": {"type": "string", "description": "新任务简报（可选）"},
            "turn_type": {
                "type": "string",
                "enum": ["plan", "execute", "followup"],
                "description": "新轮次类型（可选）",
            },
        },
        "required": ["work_item_id"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        self.store = store
        self.org_engine = org_engine

    def bind_deps(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        """后绑定依赖——公司链路装配点注入；None 保持 fail-closed。"""
        if store is not None:
            self.store = store
        if org_engine is not None:
            self.org_engine = org_engine

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        if self.store is None or self.org_engine is None:
            return _err("modify_work_item requires store/org_engine deps")
        work_item_id = str(params.get("work_item_id") or "").strip()
        if not work_item_id:
            return _err("modify_work_item requires work_item_id")
        child, parent, err = _child_of(self.store, work_item_id)
        if err:
            return _err(err)
        assert child is not None and parent is not None
        caller = _caller_role(context)
        if not _owns(parent, caller):
            return _err("caller is not the owner of this child work item")
        if child.phase in _REVIEW_PHASES:
            return _err(f"cannot modify work item in review phase ({child.phase.value})")
        if child.phase not in _MUTABLE_PHASES:
            return _err(f"cannot modify work item in phase {child.phase.value}")
        updated: dict[str, Any] = {}
        title = str(params.get("title") or "").strip()
        description = str(params.get("description") or "").strip()
        turn_type_raw = str(params.get("turn_type") or "").strip().lower()
        if title:
            child.title = title
            updated["title"] = title
        if description:
            child.description = description
            updated["description"] = description
        if turn_type_raw:
            turn_type = _TURN_TYPE_MAP.get(turn_type_raw)
            if turn_type is None:
                return _err(f"invalid turn_type '{turn_type_raw}' (plan|execute|followup)")
            child.turn_type = turn_type
            updated["turn_type"] = turn_type_raw
        if not updated:
            return _err("modify_work_item requires at least one of title/description/turn_type")
        self.store.save(child)
        _record_parent_mutation(self.store, parent, modified_id=child.id)
        return {"success": True, "work_item_id": child.id, "updated": updated}


class DeleteWorkItemTool(BaseTool):
    name: str = "delete_work_item"
    description: str = (
        "Cancel a child WorkItem you own that is no longer needed (soft-cancel; audit is "
        "preserved). Rejects running cards and cards that still have live children. Removes "
        "the card from the parent dependency barrier so synthesis is not blocked."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "work_item_id": {"type": "string", "description": "要取消的子卡 id"},
            "reason": {"type": "string", "description": "取消原因（可选）"},
        },
        "required": ["work_item_id"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        self.store = store
        self.org_engine = org_engine

    def bind_deps(self, store: Any | None = None, org_engine: Any | None = None) -> None:
        """后绑定依赖——公司链路装配点注入；None 保持 fail-closed。"""
        if store is not None:
            self.store = store
        if org_engine is not None:
            self.org_engine = org_engine

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        if self.store is None or self.org_engine is None:
            return _err("delete_work_item requires store/org_engine deps")
        work_item_id = str(params.get("work_item_id") or "").strip()
        if not work_item_id:
            return _err("delete_work_item requires work_item_id")
        child, parent, err = _child_of(self.store, work_item_id)
        if err:
            return _err(err)
        assert child is not None and parent is not None
        caller = _caller_role(context)
        if not _owns(parent, caller):
            return _err("caller is not the owner of this child work item")
        if child.phase in _TERMINAL_PHASES:
            return _err(f"work item already terminal ({child.phase.value})")
        if child.phase in _REVIEW_PHASES:
            return _err(f"cannot delete work item in review phase ({child.phase.value})")
        if child.phase not in _MUTABLE_PHASES:
            return _err(f"cannot delete work item in phase {child.phase.value}")
        live_children = [
            item.id
            for item in self.store.list_all()
            if str(getattr(item, "parent_id", "") or "") == child.id
            and item.phase not in _TERMINAL_PHASES
        ]
        if live_children:
            return _err(
                "work item still has live children; delete them first: " + ", ".join(live_children)
            )
        reason = str(params.get("reason") or "").strip() or "manager delete_work_item"
        transition_work_item(child, Phase.CANCELLED, self.store, reason=reason)
        metadata = dict(parent.metadata or {})
        metadata[_DEPENDENCY_IDS_KEY] = [
            str(v) for v in (metadata.get(_DEPENDENCY_IDS_KEY) or []) if str(v) != child.id
        ]
        parent.metadata = metadata
        _record_parent_mutation(self.store, parent, deleted_id=child.id)
        return {"success": True, "work_item_id": child.id, "phase": "cancelled"}
