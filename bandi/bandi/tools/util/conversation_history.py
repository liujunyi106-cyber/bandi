# 功能：conversation_history — 会话历史读取（SessionStore 适配；缺省回落当前会话 + 角色过滤 + 截断）
from __future__ import annotations

from typing import Any

from bandi.memory.session.store import SessionStore
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_DEFAULT_ROLES: tuple[str, ...] = ("user", "assistant")


def _truncate(text: str, max_chars: int) -> str:
    """功能：超长内容截断并在末尾补 … 省略符（省略符计 1 字符）。"""
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


class ConversationHistoryTool(BaseTool):
    """功能：按会话读取消息历史，支持角色过滤、数量限制、单条字符截断。"""

    name = "conversation_history"
    description = "会话历史：get（缺省回落当前 session_id，角色 user/assistant，可截断）"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["get"], "default": "get"},
            "session_id": {"type": "string", "description": "目标会话（缺省用 context 当前会话）"},
            "role": {"type": "string", "description": "仅返回该角色（user/assistant/tool）"},
            "limit": {"type": "integer", "description": "最多返回条数"},
            "max_chars_per_message": {"type": "integer", "description": "单条截断字符数"},
        },
        "required": ["operation"],
    }

    def __init__(self, session_db_path: str | None = None) -> None:
        self._db_path = session_db_path or _default_session_db()
        self._store: SessionStore | None = None

    def _get_store(self) -> SessionStore:
        store = self._store
        if store is None:
            store = SessionStore(self._db_path)
            self._store = store
        return store

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation", "get")
        if op != "get":
            raise ValueError(f"Unknown operation: {op}")
        extra: dict[str, Any] = {}
        if context is not None:
            extra = getattr(context, "extra", None) or {}
        session_id = params.get("session_id") or extra.get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id is required"}
        store = self._get_store()
        entries = store.get_branch(session_id)
        role = params.get("role")
        roles: tuple[str, ...] | str = (role,) if role else _DEFAULT_ROLES
        max_chars = params.get("max_chars_per_message")
        limit = params.get("limit")
        messages = []
        truncated = False
        for e in reversed(entries):
            if e.get("role") not in roles:
                continue
            content = e.get("content", "") or ""
            if max_chars is not None:
                trimmed = _truncate(content, int(max_chars))
                if trimmed != content:
                    truncated = True
                content = trimmed
            messages.append({"role": e.get("role"), "content": content})
        if limit is not None:
            messages = messages[: int(limit)]
        return {
            "ok": True,
            "session_id": session_id,
            "messages": messages,
            "count": len(messages),
            "truncated": truncated,
        }


def _default_session_db() -> str:
    # 库不同源，工具恒读空库且会在 data 目录生成空 sessions.db（静默失效）
    from pathlib import Path

    return str(Path.home() / ".bandi" / "data" / "bandi.db")
