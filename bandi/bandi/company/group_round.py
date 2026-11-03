# 功能：群聊回合状态模型——slot 顺序、seen 游标、上下文渲染与持久化往返
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bandi.company.group_chat_protocol import GROUP_PASS_HINT, is_pass_output

DEFAULT_RELAY_PER_MEMBER = 3
DEFAULT_CONTEXT_LINES = 24
DEFAULT_CONTEXT_MAX_CHARS = 6000
GROUP_HANDOFF_TOKEN_THRESHOLD = 6000
GROUP_HANDOFF_KEEP_LINES = 8
GROUP_HANDOFF_NOTE_MAX_CHARS = 4000


class RoundStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SlotStatus(StrEnum):
    WAITING = "waiting"
    SPEAKING = "speaking"
    POSTED = "posted"
    PASSED = "passed"
    MISSED = "missed"
    WORKING = "working"
    WAITING_APPROVAL = "waiting_approval"
    FAILED = "failed"
    LEFT = "left"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class SlotOrigin(StrEnum):
    USER = "user"
    BOT = "bot"


TERMINAL_SLOT_STATUSES = frozenset(
    {
        SlotStatus.POSTED,
        SlotStatus.PASSED,
        SlotStatus.FAILED,
        SlotStatus.LEFT,
        SlotStatus.CANCELLED,
    }
)


def default_relay_limit(member_count: int) -> int:
    """功能：群聊接力上限——成员数 ×3，至少 1。"""
    return max(1, DEFAULT_RELAY_PER_MEMBER * max(0, int(member_count)))


@dataclass
class RoundLine:
    seq: int
    role_id: str
    kind: str
    text: str
    work_item_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "role_id": self.role_id,
            "kind": self.kind,
            "text": self.text,
            "work_item_id": self.work_item_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RoundLine:
        return cls(
            seq=int(payload.get("seq") or 0),
            role_id=str(payload.get("role_id") or ""),
            kind=str(payload.get("kind") or ""),
            text=str(payload.get("text") or ""),
            work_item_id=str(payload.get("work_item_id") or ""),
            created_at=float(payload.get("created_at") or time.time()),
        )


@dataclass
class RoundSlot:
    slot_id: str
    role_id: str
    status: SlotStatus = SlotStatus.WAITING
    origin: SlotOrigin = SlotOrigin.USER
    called_by: str = ""
    note: str = ""
    work_item_id: str = ""
    order: int = 0
    started_at: float = 0.0
    last_activity_at: float = 0.0
    has_tool_activity: bool = False
    yielded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "role_id": self.role_id,
            "status": self.status.value,
            "origin": self.origin.value,
            "called_by": self.called_by,
            "note": self.note,
            "work_item_id": self.work_item_id,
            "order": self.order,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "has_tool_activity": self.has_tool_activity,
            "yielded": self.yielded,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RoundSlot:
        try:
            status = SlotStatus(str(payload.get("status") or SlotStatus.WAITING.value))
        except ValueError:
            status = SlotStatus.WAITING
        try:
            origin = SlotOrigin(str(payload.get("origin") or SlotOrigin.USER.value))
        except ValueError:
            origin = SlotOrigin.USER
        return cls(
            slot_id=str(payload.get("slot_id") or uuid.uuid4().hex),
            role_id=str(payload.get("role_id") or ""),
            status=status,
            origin=origin,
            called_by=str(payload.get("called_by") or ""),
            note=str(payload.get("note") or ""),
            work_item_id=str(payload.get("work_item_id") or ""),
            order=int(payload.get("order") or 0),
            started_at=float(payload.get("started_at") or 0.0),
            last_activity_at=float(payload.get("last_activity_at") or 0.0),
            has_tool_activity=bool(payload.get("has_tool_activity")),
            yielded=bool(payload.get("yielded")),
        )


@dataclass
class GroupRound:
    round_id: str
    session_id: str
    project_id: str
    user_input: str = ""
    sender_role: str = "user"
    member_role_ids: list[str] = field(default_factory=list)
    thread_id: str = ""
    mode: str = ""
    intent: str = "task"
    status: RoundStatus = RoundStatus.RUNNING
    slots: list[RoundSlot] = field(default_factory=list)
    log: list[RoundLine] = field(default_factory=list)
    seen: dict[str, int] = field(default_factory=dict)
    relay_count: int = 0
    relay_limit: int = 0
    handoff_note: str = ""
    handoff_upto_seq: int = 0
    revision: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.relay_limit <= 0:
            count = len(self.member_role_ids) or len(self.slots)
            self.relay_limit = default_relay_limit(count)

    @property
    def active_slot(self) -> RoundSlot | None:
        for slot in self.slots:
            if slot.status == SlotStatus.SPEAKING:
                return slot
        return None

    @property
    def next_waiting_slot(self) -> RoundSlot | None:
        for slot in self.slots:
            if slot.status == SlotStatus.WAITING:
                return slot
        return None

    @property
    def is_complete(self) -> bool:
        return all(slot.status in TERMINAL_SLOT_STATUSES for slot in self.slots)

    def add_line(
        self,
        role_id: str,
        kind: str,
        text: str,
        work_item_id: str = "",
    ) -> RoundLine:
        line = RoundLine(
            seq=len(self.log) + 1,
            role_id=role_id,
            kind=kind,
            text=text,
            work_item_id=work_item_id,
        )
        self.log.append(line)
        self.updated_at = time.time()
        return line

    def mark_seen(self, role_id: str) -> int:
        seq = len(self.log)
        self.seen[role_id] = seq
        return seq

    def unseen_lines(self, role_id: str, limit: int = DEFAULT_CONTEXT_LINES) -> list[RoundLine]:
        after = int(self.seen.get(role_id, 0) or 0)
        if self.handoff_note:
            after = max(after, int(self.handoff_upto_seq or 0))
        lines = [line for line in self.log if line.seq > after and line.role_id != role_id]
        if limit > 0:
            lines = lines[-limit:]
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "user_input": self.user_input,
            "sender_role": self.sender_role,
            "member_role_ids": list(self.member_role_ids),
            "thread_id": self.thread_id,
            "mode": self.mode,
            "intent": self.intent,
            "status": self.status.value,
            "slots": [slot.to_dict() for slot in self.slots],
            "log": [line.to_dict() for line in self.log],
            "seen": dict(self.seen),
            "relay_count": self.relay_count,
            "relay_limit": self.relay_limit,
            "handoff_note": self.handoff_note,
            "handoff_upto_seq": self.handoff_upto_seq,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GroupRound:
        try:
            status = RoundStatus(str(payload.get("status") or RoundStatus.RUNNING.value))
        except ValueError:
            status = RoundStatus.RUNNING
        return cls(
            round_id=str(payload.get("round_id") or uuid.uuid4().hex),
            session_id=str(payload.get("session_id") or ""),
            project_id=str(payload.get("project_id") or ""),
            user_input=str(payload.get("user_input") or ""),
            sender_role=str(payload.get("sender_role") or "user"),
            member_role_ids=[str(item) for item in (payload.get("member_role_ids") or [])],
            thread_id=str(payload.get("thread_id") or ""),
            mode=str(payload.get("mode") or ""),
            intent=str(payload.get("intent") or "task"),
            status=status,
            slots=[
                RoundSlot.from_dict(item)
                for item in (payload.get("slots") or [])
                if isinstance(item, dict)
            ],
            log=[
                RoundLine.from_dict(item)
                for item in (payload.get("log") or [])
                if isinstance(item, dict)
            ],
            seen={
                str(key): int(value)
                for key, value in (payload.get("seen") or {}).items()
                if str(key)
            },
            relay_count=int(payload.get("relay_count") or 0),
            relay_limit=int(payload.get("relay_limit") or 0),
            handoff_note=str(payload.get("handoff_note") or ""),
            handoff_upto_seq=int(payload.get("handoff_upto_seq") or 0),
            revision=int(payload.get("revision") or 0),
            created_at=float(payload.get("created_at") or time.time()),
            updated_at=float(payload.get("updated_at") or time.time()),
        )


def slot_label(slot: RoundSlot, name: str) -> str:
    """功能：状态条/上下文里的 slot 可读标签。"""
    labels = {
        SlotStatus.SPEAKING: "正在发言",
        SlotStatus.POSTED: "已发言",
        SlotStatus.PASSED: "跳过",
        SlotStatus.MISSED: "未回应",
        SlotStatus.WORKING: "执行中",
        SlotStatus.WAITING_APPROVAL: "等审批",
        SlotStatus.FAILED: "失败",
        SlotStatus.LEFT: "已离开",
        SlotStatus.PAUSED: "已暂停",
        SlotStatus.CANCELLED: "已取消",
        SlotStatus.WAITING: "等待",
    }
    return f"{name}（{labels.get(slot.status, slot.status.value)}）"


class GroupRoundService:
    """功能：群聊回合编排——懒建卡、串行推进、PASS、接力、暂停恢复与迟到补发。"""

    def __init__(
        self,
        round_store: Any,
        work_items: Any,
        org_engine: Any | None = None,
        *,
        mention_resolver: Any | None = None,
        broadcast: Any | None = None,
        clock: Any | None = None,
        relay_limit: int | None = None,
        timeout_seconds: float = 240.0,
        work_grace_seconds: float = 15.0,
        handoff_summarizer: Any | None = None,
    ) -> None:
        self._rounds = round_store
        self._items = work_items
        self._org = org_engine
        self._mention_resolver = mention_resolver
        self._broadcast = broadcast
        self._clock = clock or time.time
        self._relay_limit = relay_limit
        self._timeout = max(1.0, float(timeout_seconds))
        self._work_grace = max(1.0, float(work_grace_seconds))
        self._progress_saved_at: dict[str, float] = {}
        self._handoff_summarizer = handoff_summarizer

    def get(self, round_id: str) -> GroupRound | None:
        return self._rounds.get(round_id)

    def get_active(self, session_id: str) -> GroupRound | None:
        return self._rounds.get_active_by_session(session_id)

    def submit_user_message(
        self,
        *,
        content: str,
        sender_role: str,
        targets: list[str],
        session_id: str,
        project_id: str,
        member_role_ids: set[str] | list[str] | None = None,
        thread_id: str = "",
        mode: str = "",
        intent: str = "task",
        default_role: str = "",
    ) -> list[Any]:
        """功能：用户消息进入当前回合（无则新建），只创建第一位发言人的卡。"""
        text = (content or "").strip()
        members = sorted({str(item) for item in (member_role_ids or []) if str(item)})
        wanted = [str(item) for item in targets if str(item)]
        if members:
            wanted = [item for item in wanted if item in members]
        if not wanted and str(default_role or "").strip():
            wanted = [str(default_role).strip()]
        if not wanted:
            return []
        if not text:
            text = "请发言"
        active = self._rounds.get_active_by_session(session_id)
        if active is not None and active.status in (RoundStatus.RUNNING, RoundStatus.PAUSED):
            if active.status == RoundStatus.PAUSED:
                active.status = RoundStatus.RUNNING
                active.relay_count = 0
                for slot in active.slots:
                    if slot.status == SlotStatus.PAUSED:
                        slot.status = SlotStatus.WAITING
            active.add_line(sender_role, "user", text)
            active.relay_count = 0
            insert_at = len(active.slots)
            current = active.active_slot
            if current is not None:
                insert_at = active.slots.index(current) + 1
            for role_id in wanted:
                waiting = next(
                    (
                        slot
                        for slot in active.slots
                        if slot.role_id == role_id and slot.status == SlotStatus.WAITING
                    ),
                    None,
                )
                if waiting is not None:
                    waiting.note = text
                    continue
                active.slots.insert(
                    insert_at,
                    RoundSlot(
                        slot_id=uuid.uuid4().hex,
                        role_id=role_id,
                        origin=SlotOrigin.USER,
                        note=text,
                    ),
                )
                insert_at += 1
            self._renumber(active)
            self._rounds.save(active)
            card = self._advance(active)
            return [card] if card is not None else []
        previous = self._rounds.list_by_session(session_id, limit=1)
        previous_note = previous[0].handoff_note if previous else ""
        round_ = GroupRound(
            round_id=uuid.uuid4().hex,
            session_id=session_id,
            project_id=project_id,
            user_input=text,
            sender_role=sender_role,
            member_role_ids=members,
            thread_id=thread_id,
            mode=mode,
            intent=intent,
            handoff_note=previous_note,
        )
        round_.relay_limit = self._relay_limit or default_relay_limit(len(members) or len(wanted))
        round_.slots = [
            RoundSlot(
                slot_id=uuid.uuid4().hex,
                role_id=role_id,
                order=index,
                note=text,
            )
            for index, role_id in enumerate(wanted)
        ]
        round_.add_line(sender_role, "user", text)
        self._rounds.save(round_)
        card = self._advance(round_)
        return [card] if card is not None else []

    def delete_session_rounds(self, session_id: str) -> int:
        """功能：会话删除后清理其回合记录，避免孤儿回合被 tick 再次推进。"""
        delete = getattr(self._rounds, "delete_by_session", None)
        if not callable(delete):
            return 0
        return int(delete(session_id))

    def context_for(self, work_item: Any) -> str:
        """功能：按卡的 round/slot 渲染本轮上下文；无回合元数据返回空。"""
        metadata = getattr(work_item, "metadata", None) or {}
        round_id = str(metadata.get("round_id") or "")
        slot_id = str(metadata.get("round_slot_id") or "")
        if not round_id or not slot_id:
            return ""
        round_ = self._rounds.get(round_id)
        if round_ is None:
            return ""
        slot = next((item for item in round_.slots if item.slot_id == slot_id), None)
        if slot is None:
            return ""
        names = {role_id: self._role_name(role_id) for role_id in round_.member_role_ids}
        return render_round_context(round_, slot, role_names=names)

    async def maybe_handoff(self, work_item: Any, result: Any) -> None:
        """功能：群聊上下文超预算时用刚发言成员的模型生成交接摘要（失败保留原文）。"""
        metadata = getattr(work_item, "metadata", None) or {}
        round_id = str(metadata.get("round_id") or "")
        round_ = self._rounds.get(round_id) if round_id else None
        if round_ is None or round_.status not in (RoundStatus.RUNNING, RoundStatus.PAUSED):
            return
        from bandi.utils.token_counter import TokenCounter

        pending = [
            line
            for line in round_.log
            if line.seq > int(round_.handoff_upto_seq or 0) and line.kind != "notice"
        ]
        if len(pending) <= GROUP_HANDOFF_KEEP_LINES:
            return
        text = "\n".join(
            f"{line.role_id}: {line.text}" for line in pending[:-GROUP_HANDOFF_KEEP_LINES]
        )
        if TokenCounter.estimate(text) < GROUP_HANDOFF_TOKEN_THRESHOLD:
            return
        role_id = next(
            (
                slot.role_id
                for slot in round_.slots
                if slot.work_item_id == str(getattr(work_item, "id", ""))
            ),
            "",
        )
        if not role_id:
            return
        summarizer = self._handoff_summarizer or _summarize_with_role_model
        try:
            summary = await summarizer(role_id, text, round_.handoff_note, self._org)
        except Exception:  # noqa: BLE001 — 摘要失败保留原文
            return
        summary = str(summary or "").strip()
        if not summary:
            return
        captured_upto = pending[-GROUP_HANDOFF_KEEP_LINES - 1].seq
        fresh = self._rounds.get(round_.round_id)
        if fresh is None or fresh.status == RoundStatus.CANCELLED:
            return
        if int(fresh.handoff_upto_seq or 0) >= captured_upto:
            return
        merged = f"{fresh.handoff_note}\n{summary}".strip()
        fresh.handoff_note = merged[-GROUP_HANDOFF_NOTE_MAX_CHARS:]
        fresh.handoff_upto_seq = captured_upto
        if not self._rounds.save_checked(fresh, fresh.revision):
            return
        self._emit(fresh)

    def on_slot_finished(self, work_item: Any, result: Any) -> None:
        """功能：卡片完成/挂起/失败后推进回合（PASS 跳过、接力插入、迟到补发）。"""
        metadata = getattr(work_item, "metadata", None) or {}
        round_id = str(metadata.get("round_id") or "")
        slot_id = str(metadata.get("round_slot_id") or "")
        if not round_id or not slot_id:
            return
        round_ = self._rounds.get(round_id)
        if round_ is None or round_.status in (RoundStatus.CANCELLED, RoundStatus.COMPLETED):
            return
        slot = next((item for item in round_.slots if item.slot_id == slot_id), None)
        if slot is None:
            return
        output = str(getattr(result, "output", "") or "")
        success = bool(getattr(result, "success", False))
        terminal_reason = str(
            (getattr(result, "metadata", None) or {}).get("terminal_reason") or ""
        )
        if slot.status in (
            SlotStatus.WORKING,
            SlotStatus.MISSED,
            SlotStatus.WAITING_APPROVAL,
        ):
            self._late_reply(
                round_,
                slot,
                output,
                str(getattr(work_item, "id", "") or ""),
                success=success,
            )
            return
        if slot.status != SlotStatus.SPEAKING:
            return
        if terminal_reason == "AWAITING_APPROVAL":
            slot.status = SlotStatus.WAITING_APPROVAL
            self._rounds.save(round_)
            self._emit(round_)
            self._advance(round_)
            return
        if not success:
            slot.status = SlotStatus.FAILED
            round_.add_line(
                "system",
                "notice",
                f"{self._role_name(slot.role_id)} 本轮未能回复：{output[:200] or '执行失败'}",
                str(getattr(work_item, "id", "") or ""),
            )
            round_.mark_seen(slot.role_id)
            self._rounds.save(round_)
            self._emit(round_)
            self._advance(round_)
            return
        if is_pass_output(output):
            slot.status = SlotStatus.PASSED
            round_.mark_seen(slot.role_id)
            self._rounds.save(round_)
            self._emit(round_)
            self._advance(round_)
            return
        slot.status = SlotStatus.POSTED
        round_.add_line(
            slot.role_id,
            "bot",
            output,
            str(getattr(work_item, "id", "") or ""),
        )
        round_.mark_seen(slot.role_id)
        self._insert_relay_slots(round_, slot, output)
        self._rounds.save(round_)
        self._emit(round_)
        self._advance(round_)

    def on_slot_interrupted(self, work_item_id: str, reason: str = "interrupted") -> None:
        """功能：卡片被中断后标记失败并推进下一位。"""
        item = self._items.get(work_item_id)
        if item is None:
            return
        metadata = getattr(item, "metadata", None) or {}
        round_id = str(metadata.get("round_id") or "")
        slot_id = str(metadata.get("round_slot_id") or "")
        round_ = self._rounds.get(round_id) if round_id else None
        if round_ is None or round_.status in (RoundStatus.CANCELLED, RoundStatus.COMPLETED):
            return
        slot = next((item for item in round_.slots if item.slot_id == slot_id), None)
        if slot is None or slot.status in TERMINAL_SLOT_STATUSES:
            return
        slot.status = SlotStatus.FAILED
        round_.add_line(
            "system",
            "notice",
            f"{self._role_name(slot.role_id)} 本轮已中断：{reason}",
            work_item_id,
        )
        self._rounds.save(round_)
        self._emit(round_)
        self._advance(round_)

    def note_progress(self, work_item_id: str, *, has_tool: bool = False) -> None:
        """功能：记录成员执行活动（超时判定与长任务让位）。"""
        item = self._items.get(work_item_id)
        if item is None:
            return
        metadata = getattr(item, "metadata", None) or {}
        round_ = self._rounds.get(str(metadata.get("round_id") or ""))
        if round_ is None:
            return
        slot = next(
            (
                slot
                for slot in round_.slots
                if slot.work_item_id == work_item_id and slot.status == SlotStatus.SPEAKING
            ),
            None,
        )
        if slot is None:
            return
        now = self._clock()
        slot.last_activity_at = now
        if has_tool:
            slot.has_tool_activity = True
        last_saved = self._progress_saved_at.get(round_.round_id, 0.0)
        if has_tool or now - last_saved >= 5.0:
            self._rounds.save(round_)
            self._progress_saved_at[round_.round_id] = now

    def tick(self, now: float | None = None) -> None:
        """功能：超时/长任务让位——把当前发言人标记为 working/missed 并推进下一位。"""
        moment = self._clock() if now is None else float(now)
        for round_ in self._rounds.list_active():
            if round_.status != RoundStatus.RUNNING:
                continue
            slot = round_.active_slot
            if slot is None:
                if round_.next_waiting_slot is not None:
                    self._advance(round_)
                elif round_.is_complete:
                    round_.status = RoundStatus.COMPLETED
                    self._rounds.save(round_)
                    self._emit(round_)
                continue
            if slot.status == SlotStatus.WAITING_APPROVAL:
                continue
            too_long = moment - (slot.last_activity_at or slot.started_at) > self._timeout
            long_work = (
                slot.has_tool_activity
                and not slot.yielded
                and moment - slot.started_at > self._work_grace
            )
            if not (too_long or long_work):
                continue
            slot.status = SlotStatus.WORKING if slot.has_tool_activity else SlotStatus.MISSED
            slot.yielded = True
            reason = (
                "执行时间较长，先让下一位发言，结果稍后补发。"
                if slot.status == SlotStatus.WORKING
                else "未在时限内回应，先让下一位发言。"
            )
            round_.add_line(
                "system",
                "notice",
                f"{self._role_name(slot.role_id)} {reason}",
                slot.work_item_id,
            )
            self._rounds.save(round_)
            self._emit(round_)
            self._advance(round_)

    def resume(self, round_id: str) -> Any | None:
        """功能：恢复被接力上限暂停的回合。"""
        round_ = self._rounds.get(round_id)
        if round_ is None or round_.status != RoundStatus.PAUSED:
            return None
        round_.status = RoundStatus.RUNNING
        round_.relay_count = 0
        for slot in round_.slots:
            if slot.status == SlotStatus.PAUSED:
                slot.status = SlotStatus.WAITING
        self._rounds.save(round_)
        self._emit(round_)
        return self._advance(round_)

    def cancel(self, round_id: str, reason: str = "cancelled") -> list[str]:
        """功能：取消回合并返回需要中断的卡 id；未开始卡直接取消。"""
        round_ = self._rounds.get(round_id)
        if round_ is None:
            return []
        round_.status = RoundStatus.CANCELLED
        ids: list[str] = []
        for slot in round_.slots:
            if slot.status in TERMINAL_SLOT_STATUSES:
                continue
            if slot.work_item_id:
                ids.append(slot.work_item_id)
            slot.status = SlotStatus.CANCELLED
        self._rounds.save(round_)
        self._emit(round_)
        return ids

    def snapshot(self, round_: GroupRound) -> dict[str, Any]:
        """功能：回合状态快照（API/WS 状态条同源）。"""
        return {
            "round_id": round_.round_id,
            "session_id": round_.session_id,
            "project_id": round_.project_id,
            "status": round_.status.value,
            "relay_count": round_.relay_count,
            "relay_limit": round_.relay_limit,
            "handoff_note": round_.handoff_note,
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "role_id": slot.role_id,
                    "role_name": self._role_name(slot.role_id),
                    "status": slot.status.value,
                    "origin": slot.origin.value,
                    "called_by": slot.called_by,
                    "order": slot.order,
                    "work_item_id": slot.work_item_id,
                }
                for slot in round_.slots
            ],
            "updated_at": round_.updated_at,
        }

    def _advance(self, round_: GroupRound) -> Any | None:
        if round_.status != RoundStatus.RUNNING:
            return None
        if round_.active_slot is not None:
            return None
        slot = round_.next_waiting_slot
        if slot is None:
            if round_.is_complete:
                round_.status = RoundStatus.COMPLETED
                self._rounds.save(round_)
                self._emit(round_)
            return None
        if slot.origin == SlotOrigin.BOT:
            if round_.relay_count >= round_.relay_limit:
                slot.status = SlotStatus.PAUSED
                round_.status = RoundStatus.PAUSED
                self._rounds.save(round_)
                self._emit(round_)
                return None
            round_.relay_count += 1
        slot.status = SlotStatus.SPEAKING
        slot.started_at = self._clock()
        slot.last_activity_at = slot.started_at
        card = self._create_card(round_, slot)
        slot.work_item_id = card.id
        self._rounds.save(round_)
        self._emit(round_)
        return card

    def _late_reply(
        self,
        round_: GroupRound,
        slot: RoundSlot,
        output: str,
        work_item_id: str,
        *,
        success: bool = True,
    ) -> None:
        if not success:
            slot.status = SlotStatus.FAILED
            round_.add_line(
                "system",
                "notice",
                f"{self._role_name(slot.role_id)} 迟到的结果失败：{output[:200] or '执行失败'}",
                work_item_id,
            )
        elif is_pass_output(output):
            slot.status = SlotStatus.PASSED
        else:
            slot.status = SlotStatus.POSTED
            round_.add_line(slot.role_id, "bot", output, work_item_id)
        round_.mark_seen(slot.role_id)
        self._rounds.save(round_)
        self._emit(round_)
        self._advance(round_)

    def _insert_relay_slots(self, round_: GroupRound, slot: RoundSlot, output: str) -> None:
        if self._mention_resolver is None:
            return
        try:
            targets = self._mention_resolver(output, list(round_.member_role_ids))
        except Exception:  # noqa: BLE001 — 接力解析失败不阻断回合
            return
        candidates: list[str] = []
        for target in targets or []:
            role_id = str(target or "").strip()
            if not role_id or role_id == slot.role_id:
                continue
            if round_.member_role_ids and role_id not in round_.member_role_ids:
                continue
            if role_id not in candidates:
                candidates.append(role_id)
        if not candidates:
            return
        insert_at = round_.slots.index(slot) + 1
        for role_id in candidates:
            if any(
                item.role_id == role_id and item.status in (SlotStatus.WAITING, SlotStatus.SPEAKING)
                for item in round_.slots
            ):
                continue
            round_.slots.insert(
                insert_at,
                RoundSlot(
                    slot_id=uuid.uuid4().hex,
                    role_id=role_id,
                    origin=SlotOrigin.BOT,
                    called_by=slot.role_id,
                ),
            )
            insert_at += 1
        self._renumber(round_)

    @staticmethod
    def _renumber(round_: GroupRound) -> None:
        for index, slot in enumerate(round_.slots):
            slot.order = index

    def _create_card(self, round_: GroupRound, slot: RoundSlot) -> Any:
        from bandi.company.work_item import (
            Phase,
            WorkItemTurnType,
            create_work_item,
        )

        metadata: dict[str, Any] = {
            "source": "chat_mention",
            "mentioned_by": round_.sender_role,
            "group_chat": True,
            "round_id": round_.round_id,
            "round_slot_id": slot.slot_id,
            "group_speaker_index": slot.order,
            "group_seen_seq": round_.seen.get(slot.role_id, 0),
            "group_origin": slot.origin.value,
            "group_called_by": slot.called_by,
            "context_hint": GROUP_PASS_HINT,
        }
        if round_.mode:
            metadata["created_mode"] = round_.mode
        if round_.intent in ("query", "confirm", "adjust"):
            metadata["chat_kind"] = round_.intent
        if round_.project_id:
            metadata["project_id"] = round_.project_id
        if round_.thread_id:
            metadata["thread_id"] = round_.thread_id
            metadata["window_id"] = round_.thread_id
        if round_.session_id:
            metadata["origin_session_id"] = round_.session_id
        if round_.intent == "task":
            allowed = self._delegate_role_ids(slot.role_id)
            if allowed:
                metadata["allowed_delegate_role_ids"] = allowed
        description = round_.user_input
        if slot.note and slot.note != round_.user_input:
            description = f"{round_.user_input}\n\n{slot.note}"
        turn_type = (
            WorkItemTurnType.INTAKE
            if round_.intent == "confirm"
            else WorkItemTurnType.PLAN
            if str(round_.mode).strip().lower() == "plan"
            else WorkItemTurnType.EXECUTE
        )
        item = create_work_item(
            title=round_.user_input[:80] or "群聊发言",
            turn_type=turn_type,
            description=description,
            assignee_role=slot.role_id,
            metadata=metadata,
            phase=Phase.QUEUED,
        )
        self._items.save(item)
        return item

    def _delegate_role_ids(self, role_id: str) -> list[str]:
        if self._org is None or role_id == "":
            return []
        allowed: list[str] = []
        getter = getattr(self._org, "get_allowed_downstream_roles", None)
        if callable(getter):
            try:
                allowed = [
                    str(item).strip() for item in (getter(role_id) or []) if str(item).strip()
                ]
            except Exception:  # noqa: BLE001 — 下游查询失败不阻断建卡
                allowed = []
        role = self._org.get_role(role_id) if hasattr(self._org, "get_role") else None
        if not allowed:
            allowed = [
                str(item).strip()
                for item in (getattr(role, "can_spawn", None) or [])
                if str(item).strip()
            ]
        if not allowed:
            return []
        manager_of = str(getattr(role, "reports_to", "") or "").strip()
        is_top = (
            bool(getattr(role, "is_final_decider", False))
            or manager_of in ("", "owner")
            or manager_of is None
        )
        return allowed if is_top else []

    def _role_name(self, role_id: str) -> str:
        if self._org is not None and hasattr(self._org, "get_role"):
            try:
                role = self._org.get_role(role_id)
            except Exception:  # noqa: BLE001 — 角色查询失败按 id 展示
                role = None
            name = str(getattr(role, "name", "") or "").strip() if role is not None else ""
            if name:
                return name
        return role_id

    def _emit(self, round_: GroupRound) -> None:
        if self._broadcast is None:
            return
        try:
            self._broadcast(self.snapshot(round_))
        except Exception:  # noqa: BLE001 — 广播失败不阻断回合
            return


def _role_employee(org_engine: Any | None, role_id: str) -> Any | None:
    """功能：按角色取在职员工（角色 1:1 占用席位，优先 list_employees）。"""
    if org_engine is None or not str(role_id or "").strip():
        return None
    lister = getattr(org_engine, "list_employees", None)
    if callable(lister):
        try:
            employees = lister(role_id=str(role_id).strip())
        except Exception:  # noqa: BLE001 — 员工查询失败回退全局模型
            employees = []
        if employees:
            return employees[0]
    getter = getattr(org_engine, "get_employee", None)
    if callable(getter):
        try:
            return getter(str(role_id).strip())
        except Exception:  # noqa: BLE001 — 员工查询失败回退全局模型
            return None
    return None


async def _summarize_with_role_model(
    role_id: str,
    text: str,
    previous_note: str,
    org_engine: Any | None,
) -> str:
    """功能：用员工当前模型把群聊较早消息压缩成交接摘要。"""
    from bandi.core.config import BandiConfig
    from bandi.llm.accounted import accounted_complete
    from bandi.llm.profiles import with_resolved_llm
    from bandi.llm.registry import ProviderRegistry

    config = with_resolved_llm(BandiConfig.load())
    updates: dict[str, Any] = {}
    employee = _role_employee(org_engine, role_id)
    llm_config = getattr(employee, "llm_config", None) if employee is not None else None
    if isinstance(llm_config, dict):
        updates = {
            key: value
            for key, value in llm_config.items()
            if value is not None and hasattr(config.llm, key)
        }
    if updates:
        config = config.model_copy(update={"llm": config.llm.model_copy(update=updates)})
    model = str(getattr(config.llm, "default_model", "") or "")
    provider = ProviderRegistry(config).get_provider(model, config, resolve_profile=False)
    messages = [
        {
            "role": "system",
            "content": (
                "你是群聊交接助手。把较早的群聊消息压缩成可继续工作的交接摘要："
                "保留目标、已完成的结论、关键数据/文件路径、未决问题与下一步；"
                "不编造，不复述寒暄。只输出摘要正文。"
            ),
        },
        {
            "role": "user",
            "content": (f"已有交接：\n{previous_note or '（无）'}\n\n本轮较早消息：\n{text}"),
        },
    ]
    response = await accounted_complete(
        provider,
        messages,
        label="group_handoff",
        session_id=f"group:{role_id}",
        max_tokens=800,
    )
    return str(getattr(response, "content", "") or "").strip()


def render_round_context(
    round_: GroupRound,
    slot: RoundSlot,
    *,
    role_names: dict[str, str] | None = None,
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> str:
    """功能：给当前发言人渲染本轮上下文（顺序 + 未读消息 + PASS 规则）。"""
    names = role_names or {}
    index = next(
        (i for i, item in enumerate(round_.slots) if item.slot_id == slot.slot_id),
        slot.order,
    )
    total = len(round_.slots)
    previous = round_.slots[index - 1] if index > 0 else None
    following = round_.slots[index + 1] if index + 1 < total else None
    previous_label = (
        slot_label(previous, names.get(previous.role_id, previous.role_id))
        if previous is not None
        else "无"
    )
    following_label = (
        slot_label(following, names.get(following.role_id, following.role_id))
        if following is not None
        else "无"
    )
    header = [
        "## 群聊回合上下文",
        f"你是本轮第 {index + 1} 位发言（共 {total} 位）。",
        f"上一位：{previous_label}；下一位：{following_label}。",
    ]
    if round_.handoff_note:
        header.append(f"前情交接：{round_.handoff_note}")
    lines = round_.unseen_lines(slot.role_id)
    rendered: list[str] = []
    for line in lines:
        speaker = names.get(line.role_id, line.role_id)
        if line.kind == "notice":
            rendered.append(f"[系统] {line.text}")
        else:
            rendered.append(f"{speaker}：{line.text}")
    body = ["本轮此前消息：", *rendered] if rendered else ["本轮暂无新消息。"]
    footer = [GROUP_PASS_HINT]
    text = "\n".join([*header, "", *body, "", *footer])
    if len(text) <= max_chars:
        return text
    # 超预算：保留头部与规则，正文从最新消息往前收。
    budget = max(500, max_chars - len("\n".join([*header, "", "", *footer])))
    kept: list[str] = []
    used = 0
    for line in reversed(rendered):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    if not kept:
        kept = ["本轮消息较长，已省略早期内容。"]
    return "\n".join([*header, "", "本轮此前消息：", *kept, "", *footer])
