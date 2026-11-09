# 功能：员工产出回写**会话历史**——写侧归属策略（幂等/跳过秘书会话/会话不存在静默跳过）。

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def session_origin(session: dict[str, Any] | None) -> str:
    """功能：会话元数据里的 origin（损坏/缺失返回空串）。"""
    raw = (session or {}).get("metadata")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("origin") or "")


def write_employee_reply_to_session(
    session_store: Any,
    session_id: str,
    *,
    sender_role: str,
    content: str,
    work_item_id: str,
    sender_name: str = "",
    process_events: list[dict[str, Any]] | None = None,
    next_assignee: dict[str, str] | None = None,
) -> str | None:
    """功能：把员工产出落进发起会话的历史（返回条目 id；跳过返回 None）。

    为什么需要：员工回复原本只写线程（threads/*.md），会话历史里没有 ⇒ 刷新/切走/
    新开会话后员工回复消失，破坏「上下文快满 → 新建会话承接」的体验。

    策略（与前端 live 注入口径一致，`ChatView` 对 `origin === "secretary"` 早退）：
      - 会话不存在（已删/伪造 id）→ 静默跳过（不 FK 崩）；
      - **项目内秘书会话**不写（秘书是通用助理对话，不承载项目团队工作流）；
      - 空内容/空 work_item_id → 跳过（幂等键不可缺）。
    幂等由 `SessionStore.append_employee_reply` 内部保证（同 work_item_id 只落一条）。
    """
    text = str(content or "").strip()
    if not str(session_id or "").strip() or not text or not str(work_item_id or "").strip():
        return None
    try:
        session = session_store.get_session(session_id)
    except Exception as exc:  # noqa: BLE001 — 观测/存储失败不阻断调度回写链
        logger.debug("session lookup failed for write-back (%s): %s", session_id, exc)
        return None
    if session is None:
        return None
    if session_origin(session) == "secretary":
        return None
    try:
        return session_store.append_employee_reply(
            session_id,
            content=text,
            sender_role=str(sender_role or ""),
            work_item_id=str(work_item_id),
            sender_name=str(sender_name or ""),
            process_events=process_events,
            next_assignee=next_assignee,
        )
    except Exception as exc:  # noqa: BLE001 — 回写失败不得冒泡到调度器
        logger.warning("employee reply write-back failed (session=%s): %s", session_id, exc)
        return None
