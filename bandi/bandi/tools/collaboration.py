# reply_message/broadcast_issue/start_meeting/respond_meeting/route_work/
from __future__ import annotations

import asyncio
import time
from typing import Any

from bandi.company.communication import CommunicationManager
from bandi.company.handoff import AgentHandoff
from bandi.company.phase import Phase
from bandi.company.work_item import WorkItemTurnType, create_work_item, transition_work_item
from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext

_MEETING_POLL_INTERVAL_SEC = 1
_HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional structured handoff: conclusion + evidence + unresolved + artifacts "
        "+ confidence (0~1). Prefer it over free text for downstream agents."
    ),
    "properties": {
        "conclusion": {"type": "string", "description": "一句话结论"},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "关键证据"},
        "unresolved": {"type": "array", "items": {"type": "string"}, "description": "未解决项"},
        "artifacts": {"type": "array", "items": {"type": "string"}, "description": "产物引用"},
        "confidence": {"type": "number", "description": "置信度 0~1"},
    },
}


def _parse_handoff(params: dict[str, Any]) -> AgentHandoff | None:
    """功能：工具入参 handoff 解析（宽松，非法即忽略不阻断发送）。"""
    raw = params.get("handoff")
    if isinstance(raw, AgentHandoff):
        return raw
    return AgentHandoff.from_dict(raw)


def _comms(context: ToolPermissionContext | None) -> CommunicationManager | None:
    if context is None:
        return None
    return getattr(context, "communication", None)


def _sender(context: ToolPermissionContext | None) -> str:
    return context.role if context is not None else ""


def _err(message: str) -> dict[str, Any]:
    return {"error": message}


class InboxTool(BaseTool):
    name: str = "inbox"
    description: str = (
        "Check the collaboration inbox for the current agent role. "
        "action=status returns the unread count; action=peek also returns message bodies."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "peek"],
                "description": "status = 未读计数；peek = 计数 + 消息内容",
            },
            "limit": {"type": "integer", "description": "最大返回条数（默认 10）"},
        },
    }
    is_concurrency_safe: bool = True
    risk_level: str = "low"
    is_read_only: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        if comms is None or not role:
            return _err("inbox requires an active communication context")
        action = str(params.get("action") or "status").strip().lower() or "status"
        if action not in ("status", "peek"):
            action = "status"
        limit = max(int(params.get("limit") or 10), 1)
        try:
            messages = comms.read_inbox(role, include_seen=False)[-limit:]
        except (OSError, ValueError) as exc:
            return _err(f"inbox read failed: {exc}")
        return {
            "action": action,
            "unread_count": len(messages),
            "messages": (
                [
                    {
                        "id": m.id,
                        "sender": m.sender_role,
                        "subject": m.subject,
                        "body": m.body,
                        "blocking": m.blocking,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages
                ]
                if action == "peek"
                else []
            ),
        }


class SendDmTool(BaseTool):
    name: str = "send_dm"
    description: str = (
        "Send a direct message to another agent role. The sender is the current "
        "agent role; the message is persisted to the recipient's inbox."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to_agent": {"type": "string", "description": "收件人角色 id"},
            "subject": {"type": "string", "description": "消息主题"},
            "body": {"type": "string", "description": "消息正文"},
            "blocking": {"type": "boolean", "description": "是否请求回复（默认 false）"},
            "handoff": _HANDOFF_SCHEMA,
        },
        "required": ["to_agent", "subject", "body"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        to_agent = str(params.get("to_agent") or "").strip()
        subject = str(params.get("subject") or "").strip()
        body = str(params.get("body") or "")
        if comms is None or not role:
            return _err("send_dm requires an active communication context")
        if not to_agent or not subject:
            return _err("send_dm requires to_agent and subject")
        try:
            msg_id = comms.send_dm(
                role,
                to_agent,
                subject,
                body,
                blocking=bool(params.get("blocking")),
                handoff=_parse_handoff(params),
            )
        except (OSError, ValueError) as exc:
            return _err(f"send_dm failed: {exc}")
        return {"msg_id": msg_id, "from_agent": role, "to_agent": to_agent, "subject": subject}


class AskPeerAndWaitTool(BaseTool):
    name: str = "ask_peer_and_wait"
    description: str = (
        "Send a question to a peer role. In company mode the runtime parks the "
        "current work item (waiting_for_peer) and wakes it with the reply — end "
        "your turn after calling. Outside company mode it polls for a reply "
        "(file-system inbox) and returns the reply text or None on timeout."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to_agent": {"type": "string", "description": "被询问角色 id"},
            "subject": {"type": "string", "description": "问题主题"},
            "body": {"type": "string", "description": "问题正文"},
            "timeout_seconds": {"type": "integer", "description": "等待上限秒数（默认 60）"},
            "handoff": _HANDOFF_SCHEMA,
        },
        "required": ["to_agent", "subject", "body"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self) -> None:
        self._store: Any | None = None
        self._work_item: Any | None = None

    def bind_company(
        self,
        store: Any | None = None,
        work_item: Any | None = None,
        org_engine: Any | None = None,
    ) -> None:
        """后绑定公司运行时依赖——有 work_item/store 时启用真挂起（parking）。"""
        if store is not None:
            self._store = store
        if work_item is not None:
            self._work_item = work_item

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        to_agent = str(params.get("to_agent") or "").strip()
        subject = str(params.get("subject") or "").strip()
        body = str(params.get("body") or "")
        timeout = max(int(params.get("timeout_seconds") or 60), 1)
        if comms is None or not role:
            return _err("ask_peer_and_wait requires an active communication context")
        if not to_agent or not subject:
            return _err("ask_peer_and_wait requires to_agent and subject")
        try:
            msg_id = comms.send_dm(
                role,
                to_agent,
                subject,
                body,
                blocking=True,
                handoff=_parse_handoff(params),
            )
        except (OSError, ValueError) as exc:
            return _err(f"ask_peer_and_wait send failed: {exc}")
        if self._park_peer_wait(role, to_agent, msg_id, subject, body, timeout):
            return {
                "msg_id": msg_id,
                "from_agent": role,
                "to_agent": to_agent,
                "requires_peer_wait": True,
                "note": (
                    "本工作项已挂起（waiting_for_peer）：对方回复到达后自动唤醒并注入回复；"
                    "请立即结束本轮，不要再轮询等待。"
                ),
            }
        reply_message = await _poll_reply(comms, role, msg_id, timeout)
        reply = reply_message.body if reply_message is not None else None
        handoff = reply_message.handoff if reply_message is not None else None
        return {
            "msg_id": msg_id,
            "from_agent": role,
            "to_agent": to_agent,
            "reply": reply,
            "handoff": handoff.to_dict() if handoff is not None else None,
        }

    def _park_peer_wait(
        self,
        role: str,
        to_agent: str,
        msg_id: str,
        subject: str,
        body: str,
        timeout: int,
    ) -> bool:
        """公司挂起：写 peer_wait 元数据 + RUNNING→WAITING_FOR_PEER；不可挂起时返回 False。"""
        item = self._work_item
        if item is None or self._store is None:
            return False
        if getattr(item, "phase", None) != Phase.RUNNING:
            return False
        if item.assignee_role and item.assignee_role.lower() == to_agent.lower():
            return False
        metadata = dict(item.metadata or {})
        metadata["peer_wait"] = {
            "kind": "peer_message",
            "message_id": msg_id,
            "waiting_on_agents": [to_agent],
            "timeout_seconds": max(timeout, 300),
            "timeout_at": time.time() + max(timeout, 300),
            "on_timeout": "continue",
            "subject": subject,
            "body": body[:500],
            "work_item_id": item.id,
        }
        item.metadata = metadata
        try:
            transition_work_item(
                item, Phase.WAITING_FOR_PEER, self._store, reason="ask_peer_and_wait: parked"
            )
        except Exception:  # noqa: BLE001 — 状态不允许挂起时回退轮询
            return False
        return True


async def _poll_reply(
    comms: CommunicationManager, asker_role: str, reply_to_id: str, timeout: int
) -> Any | None:
    """功能：轮询等待回复（返回消息本体——纯文本与结构化 handoff 一并回放）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for msg in comms.read_inbox(asker_role, include_seen=True):
                if msg.metadata.get("reply_to_msg_id") == reply_to_id:
                    return msg
        except OSError:
            pass
        await asyncio.sleep(_MEETING_POLL_INTERVAL_SEC)
    return None


class ReplyMessageTool(BaseTool):
    name: str = "reply_message"
    description: str = (
        "Reply to a previously received message. The reply is linked to the "
        "original message id and delivered to the original sender's inbox."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "被回复的原消息 id"},
            "body": {"type": "string", "description": "回复正文"},
            "subject": {"type": "string", "description": "可选主题（默认 re）"},
        },
        "required": ["message_id", "body"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        message_id = str(params.get("message_id") or "").strip()
        body = str(params.get("body") or "")
        subject = str(params.get("subject") or "")
        if comms is None or not role:
            return _err("reply_message requires an active communication context")
        if not message_id or not body:
            return _err("reply_message requires message_id and body")
        try:
            msg_id = comms.reply_to_message(message_id, role, body, subject=subject)
        except (ValueError, OSError) as exc:
            return _err(f"reply_message failed: {exc}")
        return {"msg_id": msg_id, "from_agent": role, "reply_to": message_id}


class BroadcastIssueTool(BaseTool):
    name: str = "broadcast_issue"
    description: str = (
        "Broadcast an issue/announcement to multiple agent roles at once. "
        "Each recipient receives a direct message in their inbox."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to_agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "收件人角色 id 列表",
            },
            "subject": {"type": "string", "description": "广播主题"},
            "body": {"type": "string", "description": "广播正文"},
        },
        "required": ["to_agents", "subject", "body"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        to_agents = [str(a).strip() for a in params.get("to_agents") or [] if str(a).strip()]
        subject = str(params.get("subject") or "").strip()
        body = str(params.get("body") or "")
        if comms is None or not role:
            return _err("broadcast_issue requires an active communication context")
        if not to_agents or not subject:
            return _err("broadcast_issue requires to_agents and subject")
        try:
            msg_ids = comms.broadcast(role, to_agents, subject, body)
        except (OSError, ValueError) as exc:
            return _err(f"broadcast_issue failed: {exc}")
        return {"sent": msg_ids, "from_agent": role, "to_agents": to_agents}


class StartMeetingTool(BaseTool):
    name: str = "start_meeting"
    description: str = (
        "Start a meeting room with the given participants and agenda. "
        "Returns the room id; participants are notified by direct message."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "会议主题（邀请通知用）"},
            "participants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参会角色 id 列表",
            },
            "agenda": {"type": "string", "description": "会议议程"},
            "decision_policy": {"type": "string", "description": "决策策略（默认 consensus）"},
        },
        "required": ["topic", "participants", "agenda"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        participants = [str(p).strip() for p in params.get("participants") or [] if str(p).strip()]
        agenda = str(params.get("agenda") or "").strip()
        topic = str(params.get("topic") or agenda).strip()
        if comms is None or not role:
            return _err("start_meeting requires an active communication context")
        if not participants or not agenda:
            return _err("start_meeting requires participants and agenda")
        try:
            room_id = comms.create_room(participants, agenda)
            comms.broadcast(role, participants, f"Meeting: {topic}", f"议程：{agenda}")
        except (OSError, ValueError) as exc:
            return _err(f"start_meeting failed: {exc}")
        return {
            "room_id": room_id,
            "topic": topic,
            "participants": participants,
            "agenda": agenda,
            "decision_policy": str(params.get("decision_policy") or "consensus"),
        }


class RespondMeetingTool(BaseTool):
    name: str = "respond_meeting"
    description: str = (
        "Respond to a meeting room: append a message and optionally finalize "
        "the meeting (finalize=True marks the outcome and closes the room)."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "会议房间 id"},
            "content": {"type": "string", "description": "发言内容"},
            "finalize": {"type": "boolean", "description": "是否终局（默认 false）"},
        },
        "required": ["room_id", "content"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        room_id = str(params.get("room_id") or "").strip()
        content = str(params.get("content") or "")
        if comms is None or not role:
            return _err("respond_meeting requires an active communication context")
        if not room_id or not content:
            return _err("respond_meeting requires room_id and content")
        try:
            ok = comms.respond_to_meeting(
                room_id, role, content, finalize=bool(params.get("finalize"))
            )
        except (ValueError, OSError) as exc:
            return _err(f"respond_meeting failed: {exc}")
        if not ok:
            return _err(f"meeting room {room_id} not found")
        return {"ok": True, "room_id": room_id, "from_agent": role}


class RouteWorkTool(BaseTool):
    name: str = "route_work"
    description: str = (
        "Coordinator work routing: send_followup = follow-up DM to a role; "
        "spawn_task = create a real queued work item for the target role "
        "(falls back to a high-urgency DM when no company context is bound); "
        "escalate = high-urgency reply-requested DM to the target manager role."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send_followup", "spawn_task", "escalate"],
                "description": "路由动作",
            },
            "target_role": {"type": "string", "description": "目标角色 id"},
            "prompt": {"type": "string", "description": "路由内容"},
            "priority": {"type": "string", "description": "优先级（默认 normal）"},
        },
        "required": ["action", "target_role"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(self) -> None:
        self._store: Any | None = None
        self._work_item: Any | None = None
        self._org_engine: Any | None = None

    def bind_company(
        self,
        store: Any | None = None,
        work_item: Any | None = None,
        org_engine: Any | None = None,
    ) -> None:
        """后绑定公司运行时依赖——spawn_task 据此建真实排队卡。"""
        if store is not None:
            self._store = store
        if work_item is not None:
            self._work_item = work_item
        if org_engine is not None:
            self._org_engine = org_engine

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        action = str(params.get("action") or "").strip().lower()
        target_role = str(params.get("target_role") or "").strip()
        prompt = str(params.get("prompt") or "")
        priority = str(params.get("priority") or "normal").strip()
        if comms is None or not role:
            return _err("route_work requires an active communication context")
        if not target_role:
            return _err("route_work requires target_role")
        if action == "spawn_task" and self._store is not None:
            if self._org_engine is not None:
                registered = {str(r.id).lower(): str(r.id) for r in self._org_engine.list_roles()}
                canonical_target = registered.get(target_role.lower())
                if canonical_target is None:
                    return _err(f"target_role '{target_role}' is not a registered role")
                # 派单白名单：调用者只能把卡派给自己 can_spawn 里的岗位
                if not self._org_engine.can_spawn(role, canonical_target):
                    return _err(
                        f"role '{role}' cannot spawn '{canonical_target}' (not in can_spawn)"
                    )
            parent_meta = dict(getattr(self._work_item, "metadata", {}) or {})
            metadata: dict[str, Any] = {"source": "route_work", "routed_by": role}
            for key in ("project_id", "thread_id", "window_id", "origin_session_id"):
                if parent_meta.get(key):
                    metadata[key] = parent_meta[key]
            parent_id = str(getattr(self._work_item, "id", "") or "")
            if parent_id:
                metadata["parent_id"] = parent_id
            item = create_work_item(
                title=(prompt[:60] or f"route to {target_role}"),
                turn_type=WorkItemTurnType.FOLLOWUP,
                description=prompt,
                assignee_role=target_role,
                metadata=metadata,
            )
            self._store.save(item)
            return {
                "action": action,
                "routed_to": target_role,
                "priority": priority,
                "work_item_id": item.id,
            }
        if action == "send_followup":
            subject = "Coordinator follow-up"
            blocking = False
        elif action == "spawn_task":
            subject = "New task assignment"
            blocking = False
        elif action == "escalate":
            subject = "Escalation from coordinator"
            blocking = True
        else:
            return _err(f"unknown action: {action}. Valid: send_followup, spawn_task, escalate")
        try:
            msg_id = comms.send_dm(role, target_role, subject, prompt, blocking=blocking)
        except (OSError, ValueError) as exc:
            return _err(f"route_work failed: {exc}")
        return {
            "action": action,
            "routed_to": target_role,
            "priority": priority,
            "msg_id": msg_id,
        }


class CloseHumanReviewTool(BaseTool):
    name: str = "close_human_review"
    description: str = (
        "Close a human review cycle: deliver a closure notification to the "
        "target role (default owner) with the review summary and user message."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "评审关闭摘要"},
            "user_message": {"type": "string", "description": "用户补充消息（可选）"},
            "target_role": {"type": "string", "description": "通知对象角色（默认 owner）"},
        },
        "required": ["summary"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        comms = _comms(context)
        role = _sender(context)
        summary = str(params.get("summary") or "").strip()
        user_message = str(params.get("user_message") or "").strip()
        target_role = str(params.get("target_role") or "owner").strip()
        if comms is None or not role:
            return _err("close_human_review requires an active communication context")
        # 与 dispatch 路径同授权口径：人工评审关闭必须由系统上下文显式授权
        # （ToolPermissionContext.extra 注入）——agent 自行调用不得绕过审批
        extra = getattr(context, "extra", None) or {}
        if not extra.get("human_review_close_allowed"):
            return _err(
                "close_human_review is only available during an owner-facing delivery "
                "review follow-up (requires human_review_close_allowed authorization)"
            )
        if not summary:
            return _err("close_human_review requires summary")
        body = f"Human review closed by {role}: {summary}"
        if user_message:
            body += f"\n用户补充：{user_message}"
        try:
            msg_id = comms.send_dm(role, target_role, "Human review closed", body, blocking=False)
        except (OSError, ValueError) as exc:
            return _err(f"close_human_review failed: {exc}")
        return {"ok": True, "closed_by": role, "summary": summary, "msg_id": msg_id}
