from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig

# 两个未注入 ledger 的会话（CLI 多会话 / 裸 registry 路径）互相读到对方的 todo。
_shared_ledgers: dict[str, TaskLedger] = {}


def _get_shared_ledger(session_id: str = "") -> TaskLedger:
    """功能：取该会话的共享台账（`""` = 未知/进程级兜底桶）。

    生产主链两条（API/WS 会话 + 公司员工）均由 factory 注入会话台账，走不到这里；
    本函数只服务 CLI/裸 registry 与子代理兜底路径。
    """
    key = str(session_id or "")
    ledger = _shared_ledgers.get(key)
    if ledger is None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        _shared_ledgers[key] = ledger
    return ledger


def _context_session_id(context: Any | None) -> str:
    """功能：从工具上下文取会话 id（分桶键；缺失 → 兜底桶 ""）。"""
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        return str(extra.get("session_id") or "")
    return ""


class TodoReadTool(BaseTool):
    name: str = "todo_read"
    description: str = (
        "Read the current runtime task ledger snapshot. Returns all items with their statuses."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"
    is_read_only: bool = True
    # -修复：声明需上下文 → executor 注入 extra.session_id → 共享台账按会话分桶
    needs_context: bool = True

    def __init__(self, ledger: TaskLedger | None = None) -> None:
        self._ledger = ledger

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        ledger = self._ledger or _get_shared_ledger(_context_session_id(context))
        return ledger.snapshot()


class TodoWriteTool(BaseTool):
    name: str = "todo_write"
    description: str = (
        "Create or update a structured TODO list for tracking multi-step tasks. "
        "Pass either a JSON string or a real array of items. "
        "Preferred fields are 'content', 'active_form', and 'status' "
        "('pending' | 'in_progress' | 'completed'). Fields 'id'/'title'/'status' are also accepted. "
        "Keep only ONE item 'in_progress' at a time."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "todos": {
                "oneOf": [
                    {
                        "type": "string",
                        "description": "JSON array of todo items.",
                    },
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                                "active_form": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "done"],
                                },
                            },
                        },
                    },
                ],
                "description": "TODO items to create or update",
            },
        },
        "required": ["todos"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    # -修复：声明需上下文 → executor 注入 extra.session_id → 共享台账按会话分桶
    needs_context: bool = True

    def __init__(self, ledger: TaskLedger | None = None) -> None:
        self._ledger = ledger

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        ledger = self._ledger or _get_shared_ledger(_context_session_id(context))
        return ledger.apply_todos(params.get("todos", []))
