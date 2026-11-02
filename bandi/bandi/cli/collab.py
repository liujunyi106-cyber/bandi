from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import typer
from rich.console import Console

from bandi.company.communication import CommunicationManager
from bandi.company.org_engine import OrgEngine

console = Console()
collab_app = typer.Typer(help="协作通信")
meeting_app = typer.Typer(help="会议管理")

collab_app.add_typer(meeting_app, name="meeting")

_COLLAB_ROLE_ENV = "BANDI_COMMS_FROM"
_WORKSPACE_ROOT_ENV = "BANDI_WORKSPACE_ROOT"
_PROJECT_ENV = "BANDI_COMMS_PROJECT"
_SESSION_ENV = "BANDI_COMMS_SESSION"


def _load_persisted_org() -> OrgEngine:
    """加载持久化组织（org.json）——拓扑校验需要真实角色关系；
    文件缺失/损坏时返回 builtin 默认组织。"""
    from bandi.company.org_persistence import load_org
    from bandi.company.recruiter import Recruiter

    org = OrgEngine()
    try:
        load_org(org, Recruiter())
    except (OSError, ValueError):
        pass
    return org


def _build_comms() -> CommunicationManager:
    role = os.environ.get(_COLLAB_ROLE_ENV, "")
    if not role:
        raise typer.Exit(
            f"Missing {_COLLAB_ROLE_ENV}: set current agent role (e.g. worker_1) to use collab commands"
        )
    # 默认工作区固定到 ~/.bandi/workspace（不得绑定 CWD——不同启动目录
    # 会各自建独立邮箱数据且泄漏到任意目录）
    workspace = os.environ.get(_WORKSPACE_ROOT_ENV, str(Path.home() / ".bandi" / "workspace"))
    project = os.environ.get(_PROJECT_ENV, "default")
    session = os.environ.get(_SESSION_ENV, "default")
    return CommunicationManager(workspace, project, session, org_engine=_load_persisted_org())


def _emit(data: dict, error: str | None = None) -> None:
    if error is not None:
        payload = {"error": error}
    else:
        payload = data
        inbox = _inbox_notice()
        if inbox:
            payload["mailbox_notice"] = inbox
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
    if error is not None:
        raise typer.Exit(1)


def _inbox_notice() -> dict | None:
    try:
        role = os.environ.get(_COLLAB_ROLE_ENV, "")
        if not role:
            return None
        unread = len(_build_comms().read_inbox(role, include_seen=False))
        if unread == 0:
            return None
        return {"unread_count": unread, "hint": "use 'bandi collab read' to view"}
    except Exception:  # noqa: BLE001 - 提示非关键路径
        return None


def _meetings_path(comms: CommunicationManager) -> Path:
    # 私有字段（字段改名即漂移，且违反封装）
    return (
        Path(comms.workspace)
        / ".bandi-comms"
        / comms.project_id
        / comms.session_id
        / "meetings.json"
    )


def _room_to_dict(room: object) -> dict:
    return {
        "id": room.id,
        "participants": room.participants,
        "agenda": room.agenda,
        "status": room.status.value,
        "outcome": room.outcome,
        "decision_method": room.decision_method,
        "decision_owner": room.decision_owner,
        "messages": [
            {
                "sender_role": m.sender_role,
                "recipient_role": m.recipient_role,
                "content": m.content,
                "reply_to": m.reply_to,
            }
            for m in room.messages
        ],
    }


def _save_room(comms: CommunicationManager, room: object) -> None:
    path = _meetings_path(comms)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[room.id] = _room_to_dict(room)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def _load_room(comms: CommunicationManager, room_id: str) -> object | None:
    from bandi.company.communication import DirectMessage, MeetingRoom, MeetingStatus

    path = _meetings_path(comms)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get(room_id)
    if raw is None:
        return None
    room = MeetingRoom(id=raw["id"], participants=raw["participants"], agenda=raw["agenda"])
    room.status = MeetingStatus(raw.get("status", "open"))
    room.outcome = raw.get("outcome", "")
    room.decision_method = raw.get("decision_method", "")
    room.decision_owner = raw.get("decision_owner")
    room.messages = [DirectMessage(**m) for m in raw.get("messages", [])]
    return room


@collab_app.command()
def send(
    to_agent: str = typer.Argument(..., help="收件人角色 ID"),
    subject: str = typer.Argument(..., help="主题"),
    body: str = typer.Argument(..., help="正文"),
    urgency: str = typer.Option("normal", "--urgency", help="low/normal/high/urgent"),
) -> None:
    """发送 DM（异步 mailbox，不阻塞发送方）。"""
    from bandi.company.communication import MessageUrgency

    try:
        urgency_enum = MessageUrgency(urgency)
    except ValueError:
        _emit({}, error=f"invalid urgency: {urgency}")
        return
    comms = _build_comms()
    msg_id = comms.send_dm(
        sender_role=os.environ.get(_COLLAB_ROLE_ENV, ""),
        recipient_role=to_agent,
        subject=subject,
        body=body,
        urgency=urgency_enum,
    )
    _emit({"message_id": msg_id, "delivery_mode": "async_mailbox"})


@collab_app.command()
def read(
    limit: int = typer.Option(10, "--limit", help="最大条数"),
    ack: bool = typer.Option(False, "--ack", help="显式归档为已读"),
) -> None:
    """读取收件箱未读消息（默认不隐式归档，--ack 显式归档）。"""
    role = os.environ.get(_COLLAB_ROLE_ENV, "")
    comms = _build_comms()
    unread = comms.read_inbox(role, include_seen=False)
    shown = unread[:limit]
    messages = [
        {
            "msg_id": m.id,
            "from_agent": m.sender_role,
            "subject": m.subject,
            "body": m.body,
            "urgency": m.metadata.get("urgency", "normal"),
        }
        for m in shown
    ]
    if ack:
        for m in shown:
            comms.mark_as_read(role, m.id)
    _emit(
        {
            "count": len(messages),
            "unread_count": len(unread),
            "messages": messages,
        }
    )


@collab_app.command()
def reply(
    message_id: str = typer.Argument(..., help="原消息 ID"),
    body: str = typer.Argument(..., help="回复正文"),
    subject: str = typer.Option("", "--subject", help="回复主题"),
) -> None:
    """回复消息（按 reply_to 关联，解除等待方 peer wait）。"""
    role = os.environ.get(_COLLAB_ROLE_ENV, "")
    comms = _build_comms()
    try:
        new_id = comms.reply_to_message(message_id, role, body, subject=subject)
    except ValueError as exc:
        _emit({}, error=str(exc))
        return
    _emit({"message_id": new_id, "reply_to": message_id})


@collab_app.command()
def colleagues() -> None:
    roles = [a.role_id for a in _load_persisted_org().list_agents()]
    _emit({"roles": roles, "self": os.environ.get(_COLLAB_ROLE_ENV, "")})


@meeting_app.command("start")
def meeting_start(
    topic: str = typer.Argument(..., help="会议主题"),
    participants: str = typer.Argument(..., help="参会角色（逗号分隔）"),
    agenda: str = typer.Argument(..., help="议程"),
    decision_owner: str = typer.Option("", "--decision-owner", help="决策人角色"),
    timeout_seconds: int = typer.Option(900, "--timeout", help="超时秒数"),
) -> None:
    """创建会议并挂起任务（requires_peer_wait=True）。"""
    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    role = os.environ.get(_COLLAB_ROLE_ENV, "")
    comms = _build_comms()
    participant_list = [p.strip() for p in participants.split(",") if p.strip()]
    if not participant_list:
        _emit({}, error="participants must not be empty")
        return
    task = create_work_item(
        title=f"[collab] {topic}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=role,
        phase=Phase.RUNNING,
    )
    result = comms.open_meeting_wait(
        task,
        topic=topic,
        participants=participant_list,
        agenda=agenda,
        decision_owner=decision_owner or None,
        timeout_seconds=timeout_seconds,
    )
    room = comms.get_room(result["meeting_room_id"])
    if room is not None:
        _save_room(comms, room)
    _emit(
        {
            "meeting_room_id": result["meeting_room_id"],
            "topic": topic,
            "requires_peer_wait": True,
            "reason": f"Meeting started; participants: {', '.join(participant_list)}",
        }
    )


@meeting_app.command("respond")
def meeting_respond(
    room_id: str = typer.Argument(..., help="会议房间 ID"),
    content: str = typer.Argument(..., help="发言内容"),
    finalize: bool = typer.Option(False, "--finalize", help="决策人收尾定案"),
) -> None:
    """会议发言；--finalize 仅决策人可调用。"""
    role = os.environ.get(_COLLAB_ROLE_ENV, "")
    comms = _build_comms()
    room = _load_room(comms, room_id)
    if room is None:
        _emit({}, error=f"meeting {room_id} not found")
        return
    comms.restore_room(room)
    try:
        ok = comms.respond_to_meeting_v2(room_id, role, content, finalize=finalize)
    except ValueError as exc:
        _emit({}, error=str(exc))
        return
    if not ok:
        _emit({}, error=f"meeting {room_id} not found")
        return
    _save_room(comms, comms.get_room(room_id))
    room = comms.get_room(room_id)
    _emit(
        {
            "meeting_room_id": room_id,
            "status": room.status.value if room else "unknown",
            "finalize": finalize,
        }
    )


@meeting_app.command("read")
def meeting_read(room_id: str = typer.Argument(..., help="会议房间 ID")) -> None:
    """读取会议状态与发言记录。"""
    comms = _build_comms()
    room = _load_room(comms, room_id)
    if room is None:
        _emit({}, error=f"meeting {room_id} not found")
        return
    _emit(
        {
            "meeting_room_id": room.id,
            "status": room.status.value,
            "participants": room.participants,
            "outcome": room.outcome,
            "decision_method": room.decision_method,
            "messages": [
                {"sender": m.sender_role, "content": m.content, "reply_to": m.reply_to}
                for m in room.messages
            ],
        }
    )
