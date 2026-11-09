# 功能：桥接——帧栈进度只读投影（skills 层私域 → 组织层可见性快照）。
# 隔离契约：纯读（不写不推进不落盘）；投影为**值快照**（新建 dict，不含对象引用）；
# 只传播结构化字段（技能/节点/状态/字段名），**不含槽位值/自由文本**——组织层看
# 「员工执行进度」，不见员工执行内容；skill 层执行态不受组织层任何影响（防污染）。
from __future__ import annotations

import hashlib
from typing import Any

from bandi.skills.runtime import SkillFrame, SkillFrameStack


def frame_scope_for_session(session_id: str) -> str:
    """功能：会话标识 → 合法帧 scope（统一派生标准，写入端/loop 注入/组织投影三方共享）——
    path_guard 白名单不含 ':' 而公司链路会话 id 形如 `role-session::…`（角色会话）
    直接落盘会被拒；sha256 前 16 位 + 固定前缀：稳定、合法、不暴露会话结构（隔离加分）。"""
    digest = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    return f"sop-{digest}"


def _frame_progress(frame: SkillFrame) -> dict[str, Any]:
    # 功能：单帧 → 结构化进度摘要（**不含 slots 值 / summary / 提问原文**——
    # 只含字段名与状态，员工执行数据不越层）
    return {
        "task_id": frame.task_id,
        "skill_id": frame.skill_id,
        "node_id": frame.node_id,
        "status": frame.status.value,
        "awaiting_fields": list(frame.awaiting_fields),
        "collected_fields": sorted(frame.slots.keys()),
        "updated_at": frame.updated_at,
    }


def build_sop_progress(stack: SkillFrameStack | None) -> dict[str, Any] | None:
    """功能：帧栈 → 组织层进度投影（无栈/空栈返回 None）；纯只读值快照。"""
    if stack is None:
        return None
    if stack.active is None and not stack.pending and not stack.completed:
        return None
    progress: dict[str, Any] = {
        "scope_id": stack.scope_id,
        "revision": stack.revision,
        "active": _frame_progress(stack.active) if stack.active is not None else None,
        "pending": [_frame_progress(frame) for frame in stack.pending],
        "completed_count": len(stack.completed),
    }
    if stack.completed:
        last = stack.completed[-1]
        progress["last_completed"] = {
            "task_id": last.task_id,
            "skill_id": last.skill_id,
            "updated_at": last.updated_at,
        }
    return progress
