# 功能：通信管理器——DM/会议/公告/外部代理信道统一入口

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from bandi.company.comms import (
    COMMS_ROOT_NAME,
    INBOX_DIRNAME,
    MEETINGS_DIRNAME,
    NEW_DIRNAME,
    OUTBOX_DIRNAME,
    SEEN_DIRNAME,
    CommsMessage,
    append_transcript,
    create_meeting,
    list_inbox,
    mark_read,
    send_message,
)
from bandi.company.handoff import AgentHandoff
from bandi.company.phase import Phase, validate_transition

logger = logging.getLogger(__name__)


class MessageUrgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class MessageStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    REPLIED = "replied"
    TIMED_OUT = "timed_out"


class MeetingStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DECIDED = "decided"
    CLOSED = "closed"
    CANCELLED = "cancelled"


_TERMINAL_MEETING_STATUSES = frozenset(
    {MeetingStatus.DECIDED, MeetingStatus.CLOSED, MeetingStatus.CANCELLED}
)


@dataclass
class MessageRecord:
    id: str
    sender_role: str
    recipient_roles: list[str]
    subject: str
    body: str
    urgency: MessageUrgency
    status: MessageStatus
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirectMessage:
    sender_role: str
    recipient_role: str
    content: str
    reply_to: str | None = None
    # 并发合并键（append-only union；旧文件缺省时加载侧自动补新 id）
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class MeetingRoom:
    id: str
    participants: list[str]
    agenda: str
    messages: list[DirectMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: MeetingStatus = MeetingStatus.OPEN
    current_round: int = 1
    max_rounds: int = 5
    pending_participants: list[str] = field(default_factory=list)
    consensus: bool | None = None
    outcome: str = ""
    decision_method: str = ""
    decision_owner: str | None = None
    deadline_at: float | None = None


class CommunicationManager:
    """管理 DM/会议/公告/peer wait 通信。"""

    _MAILBOX_METADATA_KEYS: tuple[str, ...] = (
        "work_item_id",
        "source_work_item_id",
        "target_work_item_id",
        "parent_work_item_id",
        "source_message_id",
        "manager_role_id",
        "manager_seat_id",
        "origin_team_instance_id",
        "target_team_instance_id",
        "target_role_id",
        "target_seat_id",
        "source_role_id",
        "source_seat_id",
        "parent_board_scope",
        "upstream_visibility",
        "release_policy",
        "reply_to_msg_id",
        "release_on_semantic_type",
        "action_hint",
        "kind",
        "route",
        "thread_id",
        "target_role",
    )
    _ENVELOPE_METADATA_KEYS: tuple[str, ...] = (
        "message_class",
        "protocol_type",
        "notification_kind",
        "actionable",
        "worker_id",
        "origin_task_id",
        "origin_projection_id",
        "origin_session_id",
    )
    _PEER_WAIT_METADATA_KEYS: tuple[str, ...] = (
        "async_mailbox",
        "reply_requested",
        "timeout_seconds",
        "legacy_blocking_request",
        "execution_task_ids",
        # 的 extra_metadata 被 _filter_metadata 过滤丢失
        "peer_timeout_escalation",
    )
    _ALLOWED_METADATA_KEYS: tuple[str, ...] = (
        *_MAILBOX_METADATA_KEYS,
        *_ENVELOPE_METADATA_KEYS,
        *_PEER_WAIT_METADATA_KEYS,
    )

    def __init__(
        self,
        workspace: str,
        project_id: str,
        session_id: str,
        org_engine: Any | None = None,
        on_message_sent: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._project_id = project_id
        self._session_id = session_id
        # 发送类操作强制校验收件人 ∈ effective_contact_roles——工具/dispatch/
        self._org_engine = org_engine
        self._on_message_sent = on_message_sent
        self._sent_messages: dict[str, MessageRecord] = {}
        self._rooms: dict[str, MeetingRoom] = {}
        self._queues: dict[str, list[DirectMessage]] = {}
        self._hydrated_ids: set[str] = set()
        self._task_adjustment_suggester: Callable[..., dict[str, Any]] | None = None

    def _assert_recipients_allowed(self, sender_role: str, recipients: list[str]) -> None:
        """收件人拓扑校验：sender 必须与全部收件人存在组织联系
        （向下 can_spawn / 向上直属 / 同 manager 同级 + 动态契约）。"""
        if self._org_engine is None or not sender_role:
            return
        from bandi.company.collaboration_policy import effective_contact_roles

        allowed = set(effective_contact_roles(sender_role, org_engine=self._org_engine))
        if not allowed:
            return
        invalid = [r for r in recipients if r and r not in allowed]
        if invalid:
            raise ValueError(
                f"Role `{sender_role}` cannot message recipient(s): {', '.join(invalid)}"
            )

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def workspace(self) -> str:
        """公开工作区路径（CLI 等外部使用者不再访问私有字段）。"""
        return self._workspace

    @property
    def session_id(self) -> str:
        """公开会话 id（CLI 等外部使用者不再访问私有字段）。"""
        return self._session_id

    @property
    def task_adjustment_suggester(self) -> Callable[..., dict[str, Any]] | None:
        return self._task_adjustment_suggester

    @task_adjustment_suggester.setter
    def task_adjustment_suggester(self, suggester: Callable[..., dict[str, Any]]) -> None:
        self._task_adjustment_suggester = suggester

    def _filter_metadata(self, extra_metadata: dict[str, str] | None) -> dict[str, str] | None:
        if not extra_metadata:
            return extra_metadata
        return {k: v for k, v in extra_metadata.items() if k in self._ALLOWED_METADATA_KEYS}

    def send_dm(
        self,
        sender_role: str,
        recipient_role: str,
        subject: str,
        body: str,
        urgency: MessageUrgency = MessageUrgency.NORMAL,
        blocking: bool = False,
        extra_metadata: dict[str, str] | None = None,
        reply_to_msg_id: str | None = None,
        message_id: str | None = None,
        handoff: AgentHandoff | None = None,
    ) -> str:
        # 的同 id 直接返回既有 id，不重复落盘（多 dispatcher/重试不产生重复消息）。
        if message_id is not None and message_id in self._sent_messages:
            return message_id
        self._assert_recipients_allowed(sender_role, [recipient_role])
        msg_id = send_message(
            workspace=self._workspace,
            project_id=self._project_id,
            session_id=self._session_id,
            sender_role=sender_role,
            recipient_role=recipient_role,
            subject=subject,
            body=body,
            blocking=blocking,
            extra_metadata=self._filter_metadata(extra_metadata),
            reply_to_msg_id=reply_to_msg_id,
            message_id=message_id,
            handoff=handoff,
        )
        record = MessageRecord(
            id=msg_id,
            sender_role=sender_role,
            recipient_roles=[recipient_role],
            subject=subject,
            body=body,
            urgency=urgency,
            status=MessageStatus.DELIVERED,
            created_at=datetime.now(UTC),
        )
        self._sent_messages[msg_id] = record
        # 回调携带过滤后 metadata（route/kind 标注），WS broadcast 可读路由语义
        filtered_metadata = self._filter_metadata(extra_metadata) or {}
        if self._on_message_sent is not None:
            try:
                self._on_message_sent(
                    {
                        "message_id": msg_id,
                        "sender_role": sender_role,
                        "recipient_role": recipient_role,
                        "subject": subject,
                        "body": body,
                        "created_at": record.created_at.isoformat(),
                        "metadata": filtered_metadata,
                    }
                )
            except Exception:  # noqa: BLE001 — 广播失败不阻断消息发送
                pass
        return msg_id

    def broadcast(
        self,
        sender_role: str,
        recipient_roles: list[str],
        subject: str,
        body: str,
        urgency: MessageUrgency = MessageUrgency.NORMAL,
        extra_metadata: dict[str, str] | None = None,
    ) -> list[str]:
        msg_ids: list[str] = []
        for role in recipient_roles:
            msg_id = self.send_dm(
                sender_role, role, subject, body, urgency, extra_metadata=extra_metadata
            )
            msg_ids.append(msg_id)
        return msg_ids

    def read_inbox(self, role_id: str, include_seen: bool = False) -> list[CommsMessage]:
        return list_inbox(
            workspace=self._workspace,
            project_id=self._project_id,
            session_id=self._session_id,
            role_id=role_id,
            include_seen=include_seen,
        )

    def mark_as_read(self, role_id: str, message_id: str) -> bool:
        if message_id in self._sent_messages:
            self._sent_messages[message_id].status = MessageStatus.READ
        return mark_read(
            workspace=self._workspace,
            project_id=self._project_id,
            session_id=self._session_id,
            role_id=role_id,
            message_id=message_id,
        )

    def get_message_status(self, message_id: str) -> MessageStatus | None:
        record = self._sent_messages.get(message_id)
        return record.status if record else None

    def _meetings_dir(self) -> Path:
        """会议房间持久化目录：<workspace>/.bandi-comms/<project>/<session>/meetings。"""
        return (
            Path(self._workspace)
            / COMMS_ROOT_NAME
            / self._project_id
            / self._session_id
            / MEETINGS_DIRNAME
        )

    @staticmethod
    def _room_to_dict(room: MeetingRoom) -> dict[str, Any]:
        return {
            "id": room.id,
            "participants": list(room.participants),
            "agenda": room.agenda,
            "status": room.status.value,
            "current_round": room.current_round,
            "max_rounds": room.max_rounds,
            "pending_participants": list(room.pending_participants),
            "consensus": room.consensus,
            "outcome": room.outcome,
            "decision_method": room.decision_method,
            "decision_owner": room.decision_owner,
            "deadline_at": room.deadline_at,
            "messages": [
                {
                    "message_id": m.message_id,
                    "sender_role": m.sender_role,
                    "recipient_role": m.recipient_role,
                    "content": m.content,
                    "reply_to": m.reply_to,
                }
                for m in room.messages
            ],
            "metadata": dict(room.metadata),
        }

    def _persist_room(self, room: MeetingRoom) -> None:
        """会议房间状态原子落盘（唯一 tmp 名；写前按 message_id union 合并盘上消息）——
        进程重启后 resolve/auto_resolve 不再因房间仅存内存而永久滞留；
        并发写者已提交的会议消息不得被本进程旧快照覆盖（append-only 语义）。"""
        try:
            directory = self._meetings_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{room.id}.json"
            disk = self._load_room_from_disk(room.id)
            if disk is not None:
                # multiset 合并（键=内容四元组，忽略 id）：旧文件消息无 id（加载期
                # 临时生成，两次加载不同），按内容计数对齐才能既防丢又不重复累积
                disk_counts = Counter(
                    (m.sender_role, m.recipient_role, m.content, m.reply_to) for m in disk.messages
                )
                extra_messages: list[DirectMessage] = []
                for message in room.messages:
                    key = (
                        message.sender_role,
                        message.recipient_role,
                        message.content,
                        message.reply_to,
                    )
                    if disk_counts.get(key, 0) > 0:
                        disk_counts[key] -= 1
                    else:
                        extra_messages.append(message)
                room.messages = list(disk.messages) + extra_messages
                if (
                    disk.status in _TERMINAL_MEETING_STATUSES
                    and room.status not in _TERMINAL_MEETING_STATUSES
                ):
                    # （内存仍进行中）不得把它拉回（LWW 会复活已决议会议、丢共识）；
                    # 终态字段以盘为准，消息仍按上面的 append-only union 合并
                    room.status = disk.status
                    room.consensus = disk.consensus
                    room.outcome = disk.outcome
                    room.decision_method = disk.decision_method
                    room.decision_owner = disk.decision_owner
                    room.current_round = max(room.current_round, disk.current_round)
                    room.pending_participants = list(disk.pending_participants)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp_path.write_text(
                json.dumps(self._room_to_dict(room), ensure_ascii=False), encoding="utf-8"
            )
            os.replace(str(tmp_path), str(path))
        except OSError as exc:
            logger.warning("meeting room %s persist failed: %s", room.id, exc)

    def _load_room_from_disk(self, room_id: str) -> MeetingRoom | None:
        try:
            path = self._meetings_dir() / f"{room_id}.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("meeting room %s restore failed: %s", room_id, exc)
            return None
        messages = [
            DirectMessage(
                sender_role=str(m.get("sender_role", "")),
                recipient_role=str(m.get("recipient_role", "")),
                content=str(m.get("content", "")),
                reply_to=m.get("reply_to"),
                message_id=str(m.get("message_id") or uuid.uuid4().hex[:12]),
            )
            for m in data.get("messages", [])
            if isinstance(m, dict)
        ]
        deadline = data.get("deadline_at")
        return MeetingRoom(
            id=str(data.get("id", room_id)),
            participants=[str(p) for p in data.get("participants", [])],
            agenda=str(data.get("agenda", "")),
            messages=messages,
            metadata=dict(data.get("metadata", {})),
            status=MeetingStatus(data.get("status", "open")),
            current_round=int(data.get("current_round", 1)),
            max_rounds=int(data.get("max_rounds", 5)),
            pending_participants=[str(p) for p in data.get("pending_participants", [])],
            consensus=data.get("consensus"),
            outcome=str(data.get("outcome", "")),
            decision_method=str(data.get("decision_method", "")),
            decision_owner=data.get("decision_owner"),
            deadline_at=float(deadline) if deadline is not None else None,
        )

    def create_room(self, participants: list[str], agenda: str) -> str:
        room_id = uuid.uuid4().hex
        self._rooms[room_id] = MeetingRoom(
            id=room_id,
            participants=participants,
            agenda=agenda,
        )
        self._persist_room(self._rooms[room_id])
        return room_id

    def get_room(self, room_id: str) -> MeetingRoom | None:
        room = self._rooms.get(room_id)
        if room is not None:
            return room
        # resolve_task_meeting_wait / auto_resolve_stale_meetings 重启后可用
        restored = self._load_room_from_disk(room_id)
        if restored is not None:
            self._rooms[room_id] = restored
        return self._rooms.get(room_id)

    def restore_room(self, room: MeetingRoom) -> None:
        """
        （跨模块封装破坏）"""
        self._rooms[room.id] = room

    def post_to_room(
        self, room_id: str, sender_role: str, content: str, reply_to: str | None = None
    ) -> bool:
        room = self._rooms.get(room_id)
        if room is None:
            return False
        msg = DirectMessage(
            sender_role=sender_role,
            recipient_role="",
            content=content,
            reply_to=reply_to,
        )
        room.messages.append(msg)
        return True

    def respond_to_meeting(
        self,
        room_id: str,
        from_agent: str,
        content: str,
        finalize: bool = False,
    ) -> bool:
        room = self.get_room(room_id)
        if room is None:
            return False
        room.messages.append(
            DirectMessage(sender_role=from_agent, recipient_role="", content=content)
        )
        if finalize:
            room.metadata["status"] = "finalized"
            # metadata["status"]，resolve_task_meeting_wait 检查 room.status
            room.status = MeetingStatus.DECIDED
            room.outcome = content
        self._persist_room(room)
        transcript_dir = (
            Path(self._workspace)
            / COMMS_ROOT_NAME
            / self._project_id
            / self._session_id
            / MEETINGS_DIRNAME
            / room_id
        )
        if not transcript_dir.exists():
            create_meeting(
                workspace=self._workspace,
                project_id=self._project_id,
                session_id=self._session_id,
                meeting_id=room_id,
                title=room.agenda,
                participants=room.participants,
            )
        append_transcript(
            workspace=self._workspace,
            project_id=self._project_id,
            session_id=self._session_id,
            meeting_id=room_id,
            speaker=from_agent,
            text=content,
        )
        return True

    def reply_to_message(
        self,
        original_msg_id: str,
        from_agent: str,
        body: str,
        subject: str = "",
        metadata: dict[str, str] | None = None,
        recipient_role: str | None = None,
        handoff: AgentHandoff | None = None,
    ) -> str:
        """

        若未显式指定 recipient，从原消息的 sender_role 推断（经文件系统查找）。
        """
        target = recipient_role
        if target is None:
            raw = self._find_message(original_msg_id)
            # 可回复，第三方不得劫持对话线程；回复目标锁定原对端
            if raw is not None and from_agent not in (raw.recipient_role, raw.sender_role):
                raise ValueError(
                    f"Role `{from_agent}` cannot reply to message {original_msg_id} "
                    f"(thread participants: {raw.sender_role}, {raw.recipient_role})"
                )
            target = raw.sender_role if raw is not None else ""
        if not target:
            raise ValueError(f"Cannot resolve recipient for message {original_msg_id}")
        merged = dict(metadata or {})
        return self.send_dm(
            sender_role=from_agent,
            recipient_role=target,
            subject=subject or "re",
            body=body,
            extra_metadata=merged,
            reply_to_msg_id=original_msg_id,
            handoff=handoff,
        )

    def _find_message(self, message_id: str) -> CommsMessage | None:
        """在收件箱目录树中定位消息文件（NEW/SEEN/OUTBOX），返回解析结果。"""
        root = (
            Path(self._workspace)
            / COMMS_ROOT_NAME
            / self._project_id
            / self._session_id
            / INBOX_DIRNAME
        )
        if not root.exists():
            return None
        for role_dir in sorted(root.iterdir()):
            if not role_dir.is_dir():
                continue
            for bucket in (NEW_DIRNAME, SEEN_DIRNAME, OUTBOX_DIRNAME):
                path = role_dir / bucket / f"{message_id}.md"
                if path.exists():
                    return CommsMessage.from_file(path)
        return None

    def open_peer_wait(
        self,
        from_agent: str,
        to_agents: list[str],
        subject: str,
        body: str,
        timeout_seconds: int = 300,
        execution_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """

        reply_requested/async_mailbox/timeout_seconds/legacy_blocking_request/
        execution_task_ids 元数据，返回立即（旧 blocking 语义用 legacy_blocking_request 标记）。
        """
        sent: list[str] = []
        for recipient in to_agents:
            msg_id = self.send_dm(
                sender_role=from_agent,
                recipient_role=recipient,
                subject=subject,
                body=body,
                blocking=False,
                extra_metadata={
                    "async_mailbox": "true",
                    "reply_requested": "true",
                    "timeout_seconds": str(timeout_seconds),
                    "legacy_blocking_request": "true",
                    "execution_task_ids": (
                        ",".join(execution_task_ids) if execution_task_ids else ""
                    ),
                },
            )
            sent.append(msg_id)
        message = None
        if sent:
            message = {
                "msg_id": sent[0],
                "from_agent": from_agent,
                "to_agents": list(to_agents),
                "subject": subject,
                "body": body,
                "blocking": False,
            }
        return {
            "requires_peer_wait": False,
            "reason": (
                f"Delivered async mailbox request to {', '.join(to_agents)}. "
                "The sender continues working without pausing."
            ),
            "message": message,
            "delivery_mode": "async_mailbox",
            "blocking_deprecated": True,
        }

    def send_dm_v2(
        self, sender: str, recipient: str, content: str, reply_to: str | None = None
    ) -> str:
        """发送 DM 到内存快路径（同时持久化到文件系统，保证 hydrate 可恢复）。

        与 send_dm 的区别：额外写入 _queues 内存队列供 read_inbox_v2 快速读取。
        持久化走 send_message（同 send_dm），保证进程重启后 hydrate() 可恢复。
        返回 msg_id 供调用方记录。
        """
        # 持久化到文件系统（与 send_dm 同路径，保证 hydrate 可恢复）
        msg_id = send_message(
            workspace=self._workspace,
            project_id=self._project_id,
            session_id=self._session_id,
            sender_role=sender,
            recipient_role=recipient,
            subject=content[:80],
            body=content,
            extra_metadata={"delivery_mode": "v2_memory"} if reply_to else None,
            reply_to_msg_id=reply_to,
        )
        # 同时入内存快路径队列（read_inbox_v2 可立即读取）
        msg = DirectMessage(
            sender_role=sender,
            recipient_role=recipient,
            content=content,
            reply_to=reply_to,
        )
        self._queues.setdefault(recipient, []).append(msg)
        return msg_id

    def read_inbox_v2(self, role: str) -> list[DirectMessage]:
        msgs = self._queues.get(role, [])
        self._queues[role] = []
        return msgs

    def hydrate(self, inbox_func: Callable[[Path], CommsMessage | None] | None = None) -> int:
        """

        遍历 inbox/*/new 目录，按 recipient_role 把未读消息入队，返回恢复条数。
        引擎启动时调用一次，恢复进程重启后的未处理消息；已恢复消息幂等跳过。
        """
        root = (
            Path(self._workspace)
            / COMMS_ROOT_NAME
            / self._project_id
            / self._session_id
            / INBOX_DIRNAME
        )
        if not root.exists():
            return 0
        restored = 0
        for role_dir in sorted(root.iterdir()):
            if not role_dir.is_dir():
                continue
            new_dir = role_dir / NEW_DIRNAME
            if not new_dir.is_dir():
                continue
            recipient = role_dir.name
            for path in sorted(new_dir.iterdir()):
                if path.suffix != ".md":
                    continue
                if path.stem in self._hydrated_ids:
                    continue
                raw: CommsMessage | None = None
                if inbox_func is not None:
                    raw = inbox_func(path)
                raw = raw if raw is not None else CommsMessage.from_file(path)
                msg = DirectMessage(
                    sender_role=raw.sender_role,
                    recipient_role=recipient,
                    content=raw.body,
                    reply_to=raw.metadata.get("reply_to_msg_id"),
                )
                self._queues.setdefault(recipient, []).append(msg)
                self._hydrated_ids.add(path.stem)
                restored += 1
        return restored

    async def reach_consensus(
        self, room_id: str, topic: str, llm: Any | None = None
    ) -> tuple[bool, str]:
        room = self._rooms.get(room_id)
        if room is None:
            raise ValueError(f"Room {room_id} not found")
        messages_text = "\n".join(f"{m.sender_role}: {m.content}" for m in room.messages)
        if not messages_text:
            return False, f"Room {room_id} has no messages"
        summary = f"Discussion about '{topic}' in room {room_id}:\n{messages_text}"
        if llm is None:
            return True, summary
        prompt = (
            f"以下是一个会议室的讨论记录，主题「{topic}」。\n"
            f"请判断讨论是否达成共识。只回答一行：以 CONSENSUS 或 NO_CONSENSUS 开头，后跟一句理由。\n\n"
            f"{messages_text}"
        )
        try:
            if asyncio.iscoroutinefunction(llm):
                verdict = await llm(prompt)
            else:
                verdict = llm(prompt)
            text = str(verdict or "").strip().upper()
            agreed = text.startswith("CONSENSUS")
            return agreed, text
        except Exception as e:  # noqa: BLE001 — LLM 判定失败不阻断会议流程
            from bandi.utils.errors import error_text

            # `return True, summary` 把判定失败默认"达成共识"，会议流程在真实
            # 分歧时被错误放行。异常仅降级为 NO_CONSENSUS，不阻断流程
            logging.getLogger(__name__).warning(
                "reach_consensus LLM verdict failed for room %s: %s", room_id, error_text(e)
            )
            return False, f"NO_CONSENSUS: consensus verdict failed ({error_text(e)})"

    def ask_peer_and_wait(
        self,
        from_agent: str,
        to_agents: list[str],
        subject: str,
        body: str,
        timeout_seconds: int = 300,
        on_timeout: str = "continue",
        manager_role: str | None = None,
        timeout_action: str = "",
    ) -> dict[str, Any]:
        """

        与 open_peer_wait（异步 mailbox）的区别：调用方收到 requires_peer_wait=True
        后必须暂停任务（Phase.WAITING_FOR_PEER），等待 resolve_task_peer_wait 解除。
        on_timeout: continue（默认，超时释放）/ manager（升级 manager 决断）/ meeting（升级会议）。
        """
        sent: list[str] = []
        for recipient in to_agents:
            msg_id = self.send_dm(
                sender_role=from_agent,
                recipient_role=recipient,
                subject=subject,
                body=body,
                blocking=True,
                extra_metadata={
                    "reply_requested": "true",
                    "timeout_seconds": str(timeout_seconds),
                },
            )
            sent.append(msg_id)
        return {
            "requires_peer_wait": True,
            "peer_wait": {
                "kind": "peer_message",
                "message_id": sent[0] if sent else "",
                "waiting_on_agents": list(to_agents),
                "timeout_action": timeout_action,
                "timeout_at": datetime.now(UTC).timestamp() + timeout_seconds,
                "on_timeout": on_timeout,
                "live_required": True,
                "manager_role": manager_role,
            },
            "message": {
                "msg_id": sent[0] if sent else "",
                "from_agent": from_agent,
                "to_agents": list(to_agents),
                "subject": subject,
                "body": body,
                "blocking": True,
            },
        }

    def _find_reply_to(
        self, message_id: str, expected_agents: list[str] | None = None
    ) -> CommsMessage | None:
        """在收件箱目录树中定位 reply_to_msg_id == message_id 的回复消息。

        expected_agents 非空时只接受来自等待对象集合内的回复——第三方消息/
        通知误触发解除必须排除（原实现任一回复即返回）。"""
        root = (
            Path(self._workspace)
            / COMMS_ROOT_NAME
            / self._project_id
            / self._session_id
            / INBOX_DIRNAME
        )
        if not root.exists():
            return None
        for role_dir in sorted(root.iterdir()):
            if not role_dir.is_dir():
                continue
            for bucket in (NEW_DIRNAME, SEEN_DIRNAME, OUTBOX_DIRNAME):
                for path in sorted((role_dir / bucket).glob("*.md")):
                    raw = CommsMessage.from_file(path)
                    if raw.metadata.get("reply_to_msg_id") != message_id:
                        continue
                    if expected_agents and raw.sender_role not in expected_agents:
                        continue
                    return raw
        return None

    @staticmethod
    def _transition_phase(task: Any, target: Phase) -> None:
        """通信驱动的 phase 变更必须走状态机合法出口（validate_transition）——
        AWAITING_HUMAN（会议转人工评审）在 ALLOWED_TRANSITIONS 无出口。
        同态转换（meeting wait 升级时 WAITING_FOR_PEER→WAITING_FOR_PEER）幂等跳过。"""
        if task.phase == target:
            return
        validate_transition(task.phase, target)
        task.phase = target

    def resolve_task_peer_wait(self, task: Any) -> bool:
        """解析等待任务：回复到达 → 解除；超时 → 按 on_timeout 三分支。返回 True=已解除可继续。"""
        wait = task.metadata.get("peer_wait")
        if not wait:
            return False
        if wait.get("kind") == "meeting":
            return False
        reply = self._find_reply_to(wait.get("message_id", ""), wait.get("waiting_on_agents"))
        now = datetime.now(UTC).timestamp()
        if reply is not None:
            task.metadata.pop("peer_wait", None)
            task.metadata.setdefault("peer_replies", []).append(
                {"from": reply.sender_role, "content": reply.body}
            )
            task.metadata["peer_wait_released"] = True
            task.metadata["latest_peer_reply"] = reply.body
            self._transition_phase(task, Phase.READY)
            return True
        # timeout_at 缺失 = 无限期等待（等回复到达或显式解除），不得视为
        # 立即超时——旧 `or 0` 默认会把新建等待当场解除（fail-open）
        timeout_at = wait.get("timeout_at")
        if timeout_at is None:
            return False
        if now < float(timeout_at):
            return False
        action = wait.get("on_timeout", "continue")
        if action == "manager":
            return self._escalate_peer_wait_to_manager(task, wait)
        if action == "meeting":
            return self._escalate_peer_wait_to_meeting(task, wait)
        task.metadata.pop("peer_wait", None)
        task.metadata["peer_timeout_action"] = "continue"
        self._transition_phase(task, Phase.READY)
        return True

    def _escalate_peer_wait_to_manager(self, task: Any, wait: dict[str, Any]) -> bool:
        manager_role = wait.get("manager_role") or task.metadata.get("manager_role_id")
        if manager_role:
            self.send_dm(
                sender_role=task.assignee_role or "system",
                recipient_role=manager_role,
                subject=f"决策请求（peer wait 超时）：{task.title}",
                body=wait.get("body", "") or "peer wait 超时，请决策",
                urgency=MessageUrgency.HIGH,
                extra_metadata={
                    "notification_kind": "decision_needed",
                    "peer_timeout_escalation": "true",
                    "work_item_id": task.id,
                },
            )
            mode = "manager"
        else:
            mode = "manager_unavailable"
        task.metadata.pop("peer_wait", None)
        task.metadata["peer_timeout_escalation"] = {"mode": mode}
        self._transition_phase(task, Phase.READY)
        return True

    def _escalate_peer_wait_to_meeting(self, task: Any, wait: dict[str, Any]) -> bool:
        participants = list(wait.get("waiting_on_agents", []))
        manager_role = wait.get("manager_role") or task.metadata.get("manager_role_id")
        if manager_role and manager_role not in participants:
            participants.append(manager_role)
        room_id = self.create_room(participants, f"peer wait 升级：{task.title}")
        wait["kind"] = "meeting"
        wait["meeting_room_id"] = room_id
        wait["escalated_from"] = "peer_message_timeout"
        task.metadata["peer_wait"] = wait
        self._transition_phase(task, Phase.WAITING_FOR_PEER)
        return False

    def _validate_blocking_recipients_ready(self, task: Any, to_agents: list[str]) -> bool:
        """轻量死锁预防：拒绝向「当前任务自身角色」发送阻塞 wait（自等待环）。"""
        if task.assignee_role and task.assignee_role in to_agents:
            return False
        return True

    def refresh_waiting_tasks(self, tasks: list[Any]) -> list[Any]:
        """批量解析 WAITING_FOR_PEER 任务（对接 heartbeat/调度扫描），返回已解除任务列表。"""
        resolved: list[Any] = []
        for task in tasks:
            if task.phase == Phase.WAITING_FOR_PEER and self.resolve_task_peer_wait(task):
                resolved.append(task)
        return resolved

    @staticmethod
    def build_waiting_graph(items: list[Any]) -> dict[str, set[str]]:
        """从共享 store 全量组装等待图：数据源是每张卡 metadata 的
        ``peer_wait.waiting_on_agents``，节点是角色——所有 dispatcher/heartbeat
        复用同一封装，避免各自内存态看到不一致的等待图。"""
        waiting_for: dict[str, set[str]] = {}
        for item in items:
            role = item.assignee_role or str(item.metadata.get("work_item_role_id", "") or "")
            wait = dict(item.metadata.get("peer_wait", {}) or {})
            targets = {str(t) for t in wait.get("waiting_on_agents", []) if t}
            if role and targets:
                waiting_for.setdefault(role, set()).update(targets)
        return waiting_for

    def detect_deadlocks(self, waiting_for: dict[str, set[str]]) -> list[tuple[str, str]]:
        """互等环检测。

        waiting_for = {role: 它正在等待的角色集合}——数据源是每张
        AWAITING_PEER 卡的 peer_wait.waiting_on_agents 元数据，节点是角色。
        A 等 B 且 B 等 A 即成环，无需查询对方运行相位（相位互斥的
        waits/running 组装方式会恒空转）。
        """
        return self._find_wait_cycles(waiting_for)

    @staticmethod
    def _find_wait_cycles(graph: dict[str, set[str]]) -> list[tuple[str, str]]:
        """DFS 三色标记找环，返回 (环起点, 环终点) 对。"""
        white, gray, black = 0, 1, 2
        color: dict[str, int] = {n: white for n in graph}
        cycles: list[tuple[str, str]] = []

        def dfs(node: str, path: list[str]) -> None:
            color[node] = gray
            for nxt in graph.get(node, ()):
                if color.get(nxt, white) == gray:
                    start = path[path.index(nxt) :] + [nxt]
                    cycles.append((start[0], start[-1]))
                elif color.get(nxt, white) == white:
                    dfs(nxt, path + [nxt])
            color[node] = black

        for node in graph:
            if color[node] == white:
                dfs(node, [node])
        return cycles

    def open_meeting_wait(
        self,
        task: Any,
        topic: str,
        participants: list[str],
        agenda: str,
        decision_owner: str | None = None,
        timeout_seconds: int = 900,
    ) -> dict[str, Any]:
        room_id = self.create_room(participants, agenda)
        room = self._rooms[room_id]
        room.status = MeetingStatus.IN_PROGRESS
        room.decision_owner = decision_owner
        room.deadline_at = datetime.now(UTC).timestamp() + timeout_seconds
        room.pending_participants = list(participants)
        # 初始态（OPEN/无 deadline），auto_resolve 超时兜底失效
        self._persist_room(room)
        task.metadata["peer_wait"] = {
            "kind": "meeting",
            "meeting_room_id": room_id,
            "waiting_on_agents": list(participants),
            "timeout_at": room.deadline_at,
            "on_timeout": "continue",
        }
        task.metadata["active_meeting"] = {
            "room_id": room_id,
            "topic": topic,
            "agenda": agenda,
            "decision_owner": decision_owner,
        }
        self._transition_phase(task, Phase.WAITING_FOR_PEER)
        return {
            "requires_peer_wait": True,
            "meeting_room_id": room_id,
            "topic": topic,
        }

    def respond_to_meeting_v2(
        self,
        room_id: str,
        from_agent: str,
        content: str,
        finalize: bool = False,
    ) -> bool:
        room = self._rooms.get(room_id)
        if room is None:
            return False
        if finalize and room.decision_owner is not None and from_agent != room.decision_owner:
            raise ValueError(
                f"Only decision owner {room.decision_owner} can finalize meeting {room_id}"
            )
        self.respond_to_meeting(room_id, from_agent, content, finalize=finalize)
        if finalize:
            room.status = MeetingStatus.DECIDED
            room.outcome = content
            room.decision_method = "manual_owner_finalize"
            room.consensus = True
        else:
            room.status = MeetingStatus.IN_PROGRESS
        return True

    def resolve_task_meeting_wait(self, task: Any) -> bool:
        """会议 wait 解除：会议 DECIDED/CLOSED 后任务可继续；human 决策则转 AWAITING_HUMAN。"""
        wait = task.metadata.get("peer_wait")
        if not wait or wait.get("kind") != "meeting":
            return False
        # get_room（非 _rooms 直查）：进程重启后从磁盘 lazy 恢复
        room = self.get_room(wait.get("meeting_room_id", ""))
        if room is None:
            return False
        if room.status not in (MeetingStatus.DECIDED, MeetingStatus.CLOSED):
            return False
        task.metadata.pop("peer_wait", None)
        task.metadata["meeting_outcome"] = room.outcome
        task.metadata["meeting_decision_method"] = room.decision_method
        task.metadata["meeting_consensus"] = room.consensus
        if task.phase != Phase.WAITING_FOR_PEER:
            # 卡已不在等待态（重派/取消等遗留的陈旧 wait）：只清理元数据
            # 不做相位迁移（否则 validate_transition 拒绝并抛穿透）
            return True
        if room.decision_method == "human_escalation":
            self._transition_phase(task, Phase.AWAITING_HUMAN)
            task.metadata["meeting_requires_human_review"] = True
        else:
            self._transition_phase(task, Phase.READY)
        return True

    def run_meeting(
        self,
        room_id: str,
        agent_respond: Callable[[str, str], str] | None = None,
        default_content: str = "同意",
    ) -> MeetingRoom | None:
        room = self._rooms.get(room_id)
        if room is None:
            return None
        if agent_respond is None:
            agent_respond = lambda _role, _room_id: default_content  # noqa: E731
        for participant in room.participants:
            if not any(m.sender_role == participant for m in room.messages):
                try:
                    content = agent_respond(participant, room_id)
                except Exception:  # 单参与者失败不阻断会议兜底
                    logger.debug(
                        "meeting participant respond failed: %s", participant, exc_info=True
                    )
                    continue
                self.respond_to_meeting_v2(room_id, participant, content)
        if room.status != MeetingStatus.DECIDED:
            room.status = MeetingStatus.DECIDED
            responded_all = all(
                any(m.sender_role == p for m in room.messages) for p in room.participants
            )
            if responded_all:
                room.decision_method = "autonomous_consensus"
                room.consensus = True
                room.outcome = room.outcome or "已达成共识"
            else:
                # 未全员响应不得虚标共识——兜底关闭如实记录（consensus=False）
                room.decision_method = "timeout_fallback"
                room.consensus = False
                room.outcome = room.outcome or "讨论超时未达成共识（自动关闭）"
        return room

    def auto_resolve_stale_meetings(self, tasks: list[Any]) -> list[str]:
        resolved: list[str] = []
        now = datetime.now(UTC).timestamp()
        for task in tasks:
            wait = task.metadata.get("peer_wait")
            if not wait or wait.get("kind") != "meeting":
                continue
            # get_room（非 _rooms 直查）：进程重启后从磁盘 lazy 恢复
            room = self.get_room(wait.get("meeting_room_id", ""))
            if room is None:
                continue
            if room.status in (MeetingStatus.DECIDED, MeetingStatus.CLOSED):
                # 会议已决议（正常 finalize / 先前超时兜底）：任务等待在此解除——
                if self.resolve_task_meeting_wait(task):
                    resolved.append(task.id)
                continue
            if room.deadline_at is not None and now >= room.deadline_at:
                self.run_meeting(room.id)
                self.resolve_task_meeting_wait(task)
                resolved.append(task.id)
        return resolved

    def add_task_comment(self, task: Any, author: str, content: str) -> bool:
        if not content:
            return False
        task.comments.append(
            {
                "author": author,
                "content": content,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    def build_agent_context(self, task: Any) -> dict[str, Any]:
        return {
            "annotations": task.comments[-10:] if getattr(task, "comments", None) else [],
        }
