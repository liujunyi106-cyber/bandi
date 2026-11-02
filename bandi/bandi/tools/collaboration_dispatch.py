from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bandi.company.collaboration_policy import collaboration_allowed
from bandi.company.communication import CommunicationManager
from bandi.company.handoff import AgentHandoff

Handler = Callable[[dict[str, Any], Mapping[str, str] | None], Any]
BoundHandler = Callable[[dict[str, Any], "CollaborationRuntimeBinding"], Any]


@dataclass
class CollaborationRuntimeContext:
    workspace: str
    project_id: str
    session_id: str
    role_id: str = ""


@dataclass
class CollaborationRuntimeBinding:
    comms: CommunicationManager
    ctx: CollaborationRuntimeContext
    allowed_tools: set[str] | None = None
    org_engine: Any = None


def _env(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get(name, default))


def _parse_allowed_tools(raw: str) -> set[str]:
    payload = str(raw or "").strip()
    if not payload:
        return set()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in payload.split(",") if item.strip()]
    if isinstance(parsed, list):
        return {str(item).strip() for item in parsed if str(item).strip()}
    return set()


async def build_collaboration_runtime_from_env(
    env: Mapping[str, str] | None = None,
    *,
    org_engine: Any = None,
) -> tuple[Any, CollaborationRuntimeContext, None, CommunicationManager]:
    """从进程环境构建协作运行时（service, context, store=None, manager）。"""
    # 解析链"空值回退默认"）——空串直通 CommunicationManager 会把邮箱
    # 数据写到进程 CWD（Path("") = cwd）
    workspace = _env("BANDI_WORKSPACE_ROOT", env=env) or str(Path.home() / ".bandi" / "company")
    project_id = _env("BANDI_COMMS_PROJECT", default="company", env=env)
    session_id = _env("BANDI_COMMS_SESSION", default="default", env=env)
    role_id = _env("BANDI_COMMS_FROM", default="", env=env)

    comms = CommunicationManager(
        workspace=workspace, project_id=project_id, session_id=session_id, org_engine=org_engine
    )
    ctx = CollaborationRuntimeContext(
        workspace=workspace,
        project_id=project_id,
        session_id=session_id,
        role_id=role_id,
    )
    return None, ctx, None, comms, org_engine


def allowed_tool_names(
    *,
    _task: Any,
    _context: CollaborationRuntimeContext,
    _manager: CommunicationManager,
    env: Mapping[str, str] | None = None,
) -> set[str]:
    explicit_raw = _env("BANDI_ALLOWED_COLLAB_TOOLS", env=env)
    explicit = _parse_allowed_tools(explicit_raw)
    # Explicitly set (even empty string or "[]" → empty set) overrides the default full list.
    # Distinguish "not set" (explicit_raw == "") from "set to empty" (explicit_raw == "[]" or " ").
    if explicit_raw.strip() != "":
        return explicit
    return set(BOUND_HANDLERS.keys())


_POLL_INTERVAL_SEC = 1


async def _poll_reply(
    comms: CommunicationManager, asker_role: str, reply_to_id: str, timeout: int
) -> Any | None:
    # 功能：轮询等待回复（返回消息本体——结构化 handoff 与纯文本一并回放）
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for msg in comms.read_inbox(asker_role, include_seen=True):
                meta = getattr(msg, "metadata", {}) or {}
                if meta.get("reply_to_msg_id") == reply_to_id:
                    return msg
        except OSError:
            pass
        await asyncio.sleep(_POLL_INTERVAL_SEC)
    return None


def _err(message: str) -> dict[str, Any]:
    return {"error": message}


async def _run_bound_handler(
    handler: BoundHandler,
    args: dict[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    org_engine: Any = None,
) -> dict[str, Any]:
    _, ctx, _, comms, _ = await build_collaboration_runtime_from_env(env, org_engine=org_engine)
    binding = CollaborationRuntimeBinding(comms=comms, ctx=ctx, org_engine=org_engine)
    try:
        result = await handler(args, binding)
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    if isinstance(result, dict):
        return result
    return {"result": result}


def _env_handler(handler: BoundHandler) -> Handler:
    async def _handler(
        args: dict[str, Any], env: Mapping[str, str] | None = None, *, org_engine: Any = None
    ) -> dict[str, Any]:
        return await _run_bound_handler(handler, args, env, org_engine=org_engine)

    return _handler


# ── bound handlers ────────────────────────────────────────────────────────────


async def _handle_inbox_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("inbox requires an active role (set BANDI_COMMS_FROM)")
    action = str(args.get("action") or "status").strip().lower() or "status"
    limit = max(int(args.get("limit") or 10), 1)
    try:
        messages = comms.read_inbox(role, include_seen=False)[-limit:]
    except (OSError, ValueError) as exc:
        return _err(f"inbox read failed: {exc}")
    return {
        "action": action,
        "unread_count": len(messages),
        "messages": [
            {
                "id": m.id,
                "sender": m.sender_role,
                "subject": m.subject,
                "body": m.body,
                "blocking": getattr(m, "blocking", False),
                "created_at": m.created_at.isoformat() if hasattr(m, "created_at") else "",
            }
            for m in messages
        ]
        if action == "peek"
        else [],
    }


async def _handle_send_dm_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("send_dm requires an active role (set BANDI_COMMS_FROM)")
    to_agent = str(args.get("to_agent") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "")
    if not to_agent or not subject:
        return _err("send_dm requires to_agent and subject")
    handoff = AgentHandoff.from_dict(args.get("handoff"))
    try:
        msg_id = comms.send_dm(
            role, to_agent, subject, body, blocking=bool(args.get("blocking")), handoff=handoff
        )
    except (OSError, ValueError) as exc:
        return _err(f"send_dm failed: {exc}")
    return {"msg_id": msg_id, "from_agent": role, "to_agent": to_agent, "subject": subject}


async def _handle_ask_peer_and_wait_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("ask_peer_and_wait requires an active role")
    to_agent = str(args.get("to_agent") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "")
    timeout = max(int(args.get("timeout_seconds") or 60), 1)
    if not to_agent or not subject:
        return _err("ask_peer_and_wait requires to_agent and subject")
    handoff = AgentHandoff.from_dict(args.get("handoff"))
    try:
        msg_id = comms.send_dm(role, to_agent, subject, body, blocking=True, handoff=handoff)
    except (OSError, ValueError) as exc:
        return _err(f"ask_peer_and_wait send failed: {exc}")
    reply_message = await _poll_reply(comms, role, msg_id, timeout)
    reply = reply_message.body if reply_message is not None else None
    reply_handoff = getattr(reply_message, "handoff", None)
    return {
        "msg_id": msg_id,
        "from_agent": role,
        "to_agent": to_agent,
        "reply": reply,
        "handoff": reply_handoff.to_dict() if reply_handoff is not None else None,
    }


async def _handle_reply_message_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("reply_message requires an active role")
    message_id = str(args.get("message_id") or "").strip()
    body = str(args.get("body") or "")
    subject = str(args.get("subject") or "")
    if not message_id or not body:
        return _err("reply_message requires message_id and body")
    try:
        msg_id = comms.reply_to_message(message_id, role, body, subject=subject)
    except (ValueError, OSError) as exc:
        return _err(f"reply_message failed: {exc}")
    return {"msg_id": msg_id, "from_agent": role, "reply_to": message_id}


async def _handle_broadcast_issue_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("broadcast_issue requires an active role")
    to_agents = [str(a).strip() for a in args.get("to_agents") or [] if str(a).strip()]
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "")
    if not to_agents or not subject:
        return _err("broadcast_issue requires to_agents and subject")
    try:
        msg_ids = comms.broadcast(role, to_agents, subject, body)
    except (OSError, ValueError) as exc:
        return _err(f"broadcast_issue failed: {exc}")
    return {"sent": msg_ids, "from_agent": role, "to_agents": to_agents}


async def _handle_start_meeting_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("start_meeting requires an active role")
    participants = [str(p).strip() for p in args.get("participants") or [] if str(p).strip()]
    agenda = str(args.get("agenda") or "").strip()
    topic = str(args.get("topic") or agenda).strip()
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
        "decision_policy": str(args.get("decision_policy") or "consensus"),
    }


async def _handle_respond_meeting_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("respond_meeting requires an active role")
    room_id = str(args.get("room_id") or "").strip()
    content = str(args.get("content") or "")
    if not room_id or not content:
        return _err("respond_meeting requires room_id and content")
    try:
        ok = comms.respond_to_meeting(room_id, role, content, finalize=bool(args.get("finalize")))
    except (ValueError, OSError) as exc:
        return _err(f"respond_meeting failed: {exc}")
    if not ok:
        return _err(f"meeting room {room_id} not found")
    room = comms.get_room(room_id)
    return {
        "ok": True,
        "room_id": room_id,
        "from_agent": role,
        "status": room.status.value if room else "unknown",
    }


async def _handle_route_work_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("route_work requires an active role")
    action = str(args.get("action") or "").strip().lower()
    target_role = str(args.get("target_role") or "").strip()
    prompt = str(args.get("prompt") or "")
    priority = str(args.get("priority") or "normal").strip()
    if not target_role:
        return _err("route_work requires target_role")
    if action == "send_followup":
        subject, blocking = "Coordinator follow-up", False
    elif action == "spawn_task":
        subject, blocking = "New task assignment", False
    elif action == "escalate":
        subject, blocking = "Escalation from coordinator", True
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


async def _handle_close_human_review_bound(
    args: dict[str, Any], binding: CollaborationRuntimeBinding
) -> dict[str, Any]:
    comms = binding.comms
    role = binding.ctx.role_id
    if not role:
        return _err("close_human_review requires an active role")
    # 评审流程显式授权关闭时可用，防止 agent 自行关闭人工评审绕过审批
    if not args.get("human_review_close_allowed"):
        return _err(
            "close_human_review is only available during an owner-facing delivery "
            "review follow-up (requires human_review_close_allowed authorization)"
        )
    summary = str(args.get("summary") or "").strip()
    user_message = str(args.get("user_message") or "").strip()
    target_role = str(args.get("target_role") or "owner").strip()
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


# ── handler registry ──────────────────────────────────────────────────────────

BOUND_HANDLERS: dict[str, BoundHandler] = {
    "inbox": _handle_inbox_bound,
    "send_dm": _handle_send_dm_bound,
    "ask_peer_and_wait": _handle_ask_peer_and_wait_bound,
    "reply_message": _handle_reply_message_bound,
    "broadcast_issue": _handle_broadcast_issue_bound,
    "start_meeting": _handle_start_meeting_bound,
    "respond_meeting": _handle_respond_meeting_bound,
    "route_work": _handle_route_work_bound,
    "close_human_review": _handle_close_human_review_bound,
}

HANDLERS: dict[str, Handler] = {
    name: _env_handler(handler) for name, handler in BOUND_HANDLERS.items()
}


async def dispatch_collaboration_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    org_engine: Any = None,
) -> tuple[dict[str, Any], bool]:
    """调用一个协作工具并返回 (result, is_error)。"""
    name = str(tool_name or "").strip()
    if name not in BOUND_HANDLERS:
        return {"error": f"unknown tool: {name}"}, True

    _, ctx, _, comms, _ = await build_collaboration_runtime_from_env(env, org_engine=org_engine)
    binding = CollaborationRuntimeBinding(
        comms=comms,
        ctx=ctx,
        allowed_tools=allowed_tool_names(_task=None, _context=ctx, _manager=comms, env=env),
        org_engine=org_engine,
    )

    allowed = binding.allowed_tools
    if allowed is not None and name not in allowed:
        return (
            {
                "error": (
                    f"tool `{name}` is not allowed for this runtime. "
                    f"Allowed tools: {', '.join(sorted(allowed)) or '(none)'}. "
                    f"Set BANDI_ALLOWED_COLLAB_TOOLS to enable."
                )
            },
            True,
        )

    handler = BOUND_HANDLERS[name]

    # org_engine 缺失时跳过（个人模式无组织拓扑，comms 层同样放行）
    if name in (
        "send_dm",
        "broadcast_issue",
        "route_work",
        "ask_peer_and_wait",
        "start_meeting",
    ):
        to_agent = str(
            args.get("to_agent") or args.get("to_agents") or args.get("target_role") or ""
        ).strip()
        if to_agent and ctx.role_id and binding.org_engine is not None:
            if not collaboration_allowed(ctx.role_id, to_agent, org=binding.org_engine):
                return (
                    {
                        "error": "permission denied: "
                        + ctx.role_id
                        + " -> "
                        + to_agent
                        + " not allowed"
                    },
                    True,
                )

    try:
        result = await handler(dict(args or {}), binding)
    except (OSError, ValueError) as exc:
        return _err(str(exc)), True

    normalized = result if isinstance(result, dict) else {"result": result}
    has_error = "error" in normalized
    return normalized, has_error
