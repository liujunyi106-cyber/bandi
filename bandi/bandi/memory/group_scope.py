# 功能：群聊记忆作用域标识——按会话派生隔离的 project_id 命名空间
from __future__ import annotations

import hashlib

GROUP_SNAPSHOT_EMPLOYEE_ID = "__group__"


def group_scope_id(conversation_id: str) -> str:
    """功能：会话 id → 群聊记忆命名空间（`group:<hash16>`）。"""
    text = str(conversation_id or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"group:{digest}"
