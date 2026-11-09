# 功能：员工单聊记忆作用域——按会话派生隔离命名空间，任务/项目单聊互不可见
from __future__ import annotations

import hashlib

DM_SNAPSHOT_EMPLOYEE_ID = "__dm__"


def dm_scope_id(project_id: str, conversation_id: str) -> str:
    """功能：单聊会话 → 独立记忆域（`dm:<hash16>`）。"""
    session = str(conversation_id or "").strip()
    if not session:
        return ""
    project = str(project_id or "").strip()
    kind = "project" if project else "task"
    raw = f"{kind}:{project}:{session}"
    return f"dm:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
