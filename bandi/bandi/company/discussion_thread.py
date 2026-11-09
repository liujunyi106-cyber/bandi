# 功能：讨论线程容器——ThreadMessage/DiscussionThread/ThreadManager + threads/ 目录持久化
# 持久化格式对齐 comms.py frontmatter 范式：frontmatter 头 + body 每行一个 JSON message。

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.company.comms import (
    THREADS_DIRNAME,
    _atomic_write_text,
    _comms_root,
    _safe_path_segment,
)
from bandi.company.handoff import AgentHandoff


@dataclass
class ThreadMessage:
    message_id: str
    sender: str
    content: str
    mentions: list[str] = field(default_factory=list)
    work_item_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 9.4：结构化交接契约（AgentHandoff.to_dict() 形态；None=纯文本消息）
    handoff: dict[str, Any] | None = None
    # 员工执行过程事件快照（回写持久化——刷新后过程块可回看；None=无过程）
    process_events: list[dict[str, Any]] | None = None
    # 系统提示元数据（note_kind / dedupe_key；普通消息为空）
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "content": self.content,
            "mentions": list(self.mentions),
            "work_item_id": self.work_item_id,
            "created_at": self.created_at.isoformat(),
            "handoff": self.handoff,
            "process_events": self.process_events,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadMessage:
        created_at = data.get("created_at")
        try:
            parsed = datetime.fromisoformat(created_at) if created_at else datetime.now(UTC)
        except ValueError:
            parsed = datetime.now(UTC)
        raw_handoff = data.get("handoff")
        handoff = raw_handoff if isinstance(raw_handoff, dict) else None
        raw_process = data.get("process_events")
        process_events = (
            [item for item in raw_process if isinstance(item, dict)]
            if isinstance(raw_process, list)
            else None
        )
        return cls(
            message_id=data.get("message_id", ""),
            sender=data.get("sender", ""),
            content=data.get("content", ""),
            mentions=list(data.get("mentions") or []),
            work_item_id=data.get("work_item_id"),
            created_at=parsed,
            handoff=handoff,
            process_events=process_events,
            metadata=dict(data.get("metadata") or {})
            if isinstance(data.get("metadata"), dict)
            else {},
        )


@dataclass
class DiscussionThread:
    thread_id: str
    participants: list[str] = field(default_factory=list)
    parent_task_id: str | None = None
    messages: list[ThreadMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 会话窗口归属：群聊线程绑定用户会话 id；单聊线程为空（窗口即 thread_id）
    session_id: str = ""
    # 线程类别：group（群聊窗口）/ dm（员工单聊窗口）/ 空（旧数据）
    kind: str = ""


def _thread_path(root: Path, thread_id: str) -> Path:
    return root / THREADS_DIRNAME / f"{_safe_path_segment(thread_id, 'thread_id')}.md"


def _serialize_messages(messages: list[ThreadMessage]) -> str:
    return "".join(json.dumps(m.to_dict(), ensure_ascii=False) + "\n" for m in messages)


def _read_disk_messages(thread_path: Path) -> list[ThreadMessage]:
    """功能：读盘上线程文件的消息列表（不存在/损坏行跳过）——并发 union 合并数据源。"""
    try:
        text = thread_path.read_text(encoding="utf-8")
    except OSError:
        return []
    _, _, body = text.partition("\n---\n")
    messages: list[ThreadMessage] = []
    for line in body.strip().split("\n"):
        if not line.strip():
            continue
        try:
            messages.append(ThreadMessage.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return messages


def _persist_thread(root: Path, thread: DiscussionThread) -> None:
    """原子全量写：frontmatter + 全部 messages（每行一个 JSON）。线程消息量级小，简单可靠。
    并发合并：写前读盘，盘上已提交消息按 message_id union 为基底（append-only 语义）——
    另一进程已写的消息不得被本进程旧快照覆盖。"""
    thread_path = _thread_path(root, thread.thread_id)
    disk_messages = _read_disk_messages(thread_path)
    if disk_messages:
        # multiset 合并（键含内容）：message_id 可能不唯一（web 链固定 "chat" id）
        # 仅"同 id 同发送者同内容"视为已落盘重复；同 id 不同内容必须保留
        disk_counts = Counter((m.message_id, m.sender, m.content) for m in disk_messages)
        extra: list[ThreadMessage] = []
        for message in thread.messages:
            key = (message.message_id, message.sender, message.content)
            if disk_counts.get(key, 0) > 0:
                disk_counts[key] -= 1
            else:
                extra.append(message)
        merged = list(disk_messages) + extra
        if merged != thread.messages:
            thread.messages = merged
    frontmatter = [
        f"thread_id: {thread.thread_id}",
        f"parent_task_id: {thread.parent_task_id or ''}",
        f"participants: {','.join(thread.participants)}",
        f"created_at: {thread.created_at.isoformat()}",
        f"session_id: {thread.session_id}",
        f"kind: {thread.kind}",
    ]
    content = "\n".join(frontmatter) + "\n---\n" + _serialize_messages(thread.messages)
    thread_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(thread_path, content)


class ThreadManager:
    """管理讨论线程：创建/追加消息/按 agent 过滤/hydrate 恢复。线程互 @ 与用户可见对话链容器。

    on_message：线程事件回调（thread_created / thread_message），沿 CommunicationManager
    on_message_sent 先例——WS 实时广播接线点；回调失败不阻断线程写（附属产物）。
    """

    def __init__(
        self,
        workspace: str,
        project_id: str,
        session_id: str,
        on_message: Any | None = None,
    ) -> None:
        self._root = _comms_root(workspace, project_id, session_id)
        self._threads: dict[str, DiscussionThread] = {}
        self._on_message = on_message

    def bind_on_message(self, on_message: Any | None) -> None:
        """后绑定事件回调（沿 subagent.bind_registry/UserInputTool.bind_ui 先例）。

        装配点（api/app.py）在 hydrate 后注入 WS 广播回调；None 注入即解除。
        """
        self._on_message = on_message

    def create_thread(
        self,
        parent_task_id: str | None = None,
        participants: list[str] | None = None,
        session_id: str = "",
        kind: str = "",
    ) -> DiscussionThread:
        thread = DiscussionThread(
            thread_id=f"thread_{uuid.uuid4().hex[:12]}",
            participants=list(participants or []),
            parent_task_id=parent_task_id,
            session_id=str(session_id or "").strip(),
            kind=str(kind or "").strip(),
        )
        self._threads[thread.thread_id] = thread
        _persist_thread(self._root, thread)
        self._emit(
            {
                "type": "thread_created",
                "thread_id": thread.thread_id,
                "participants": list(thread.participants),
                "parent_task_id": thread.parent_task_id,
                "session_id": thread.session_id,
                "kind": thread.kind,
            }
        )
        return thread

    def get_thread(self, thread_id: str) -> DiscussionThread | None:
        return self._threads.get(thread_id)

    def list_threads(self) -> list[DiscussionThread]:
        return list(self._threads.values())

    def find_thread(self, participants: list[str]) -> DiscussionThread | None:
        """功能：按参与者集合精确匹配的最新线程（9.12 归并）——连续 @ 同角色集合的
        消息并入同一对话链；无匹配返回 None（调用方新建）。"""
        target = {str(p).strip() for p in participants if str(p).strip()}
        if not target:
            return None
        candidates = [
            t for t in self._threads.values() if {str(p).strip() for p in t.participants} == target
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.created_at)

    def find_by_session(self, session_id: str) -> DiscussionThread | None:
        """功能：按群聊会话 id 找线程（会话窗口一对一）；旧线程无归属返回 None。"""
        key = str(session_id or "").strip()
        if not key:
            return None
        for thread in self._threads.values():
            if thread.session_id == key:
                return thread
        return None

    def delete_thread(self, thread_id: str) -> bool:
        """功能：删除线程（内存 + 磁盘）——删除会话窗口即断开该窗口上下文。"""
        key = str(thread_id or "").strip()
        if not key:
            return False
        existed = self._threads.pop(key, None) is not None
        try:
            _thread_path(self._root, key).unlink(missing_ok=True)
        except OSError:
            pass
        if existed:
            self._emit({"type": "thread_deleted", "thread_id": key})
        return existed

    def add_message(
        self,
        thread_id: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
        message_id: str | None = None,
        work_item_id: str | None = None,
        handoff: AgentHandoff | None = None,
        process_events: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ThreadMessage | None:
        _safe_path_segment(thread_id, "thread_id")
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        msg = ThreadMessage(
            message_id=message_id or f"m_{uuid.uuid4().hex[:12]}",
            sender=sender,
            content=content,
            mentions=list(mentions or []),
            work_item_id=work_item_id,
            handoff=handoff.to_dict() if handoff is not None else None,
            process_events=list(process_events) if process_events else None,
            metadata=dict(metadata or {}),
        )
        thread.messages.append(msg)
        _persist_thread(self._root, thread)
        self._emit(
            {
                "type": "thread_message",
                "thread_id": thread_id,
                "sender": sender,
                "content": content,
                "mentions": list(mentions or []),
                "message_id": msg.message_id,
                "work_item_id": work_item_id,
                "handoff": msg.handoff,
                "process_events": msg.process_events,
                "metadata": dict(msg.metadata),
                # 参与方集合随帧下发——前端单聊视图按 [user, target] 精确过滤
                "participants": list(thread.participants),
            }
        )
        return msg

    def _emit(self, event: dict[str, Any]) -> None:
        """触发 on_message 回调，失败不阻断线程写（附属产物，沿 on_message_sent 语义）。"""
        if self._on_message is None:
            return
        try:
            self._on_message(event)
        except Exception:  # noqa: BLE001 — 广播失败不影响线程持久化
            pass

    def get_threads_for_agent(self, agent_id: str) -> list[DiscussionThread]:
        return [t for t in self._threads.values() if agent_id in t.participants]

    def hydrate(self) -> int:
        """从 threads/ 目录恢复全部线程；损坏文件跳过并计为未恢复。返回恢复数。"""
        threads_dir = self._root / THREADS_DIRNAME
        if not threads_dir.exists():
            return 0
        count = 0
        for p in sorted(threads_dir.glob("*.md")):
            thread = self._load_file(p)
            if thread is not None:
                self._threads[thread.thread_id] = thread
                count += 1
        return count

    def _load_file(self, path: Path) -> DiscussionThread | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        front, _, body = text.partition("\n---\n")
        meta: dict[str, str] = {}
        for line in front.strip().split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        thread_id = meta.get("thread_id", path.stem)
        messages: list[ThreadMessage] = []
        body = body.strip()
        if body:
            for line in body.split("\n"):
                try:
                    messages.append(ThreadMessage.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
        created_raw = meta.get("created_at")
        try:
            created_at = datetime.fromisoformat(created_raw) if created_raw else datetime.now(UTC)
        except ValueError:
            created_at = datetime.now(UTC)
        return DiscussionThread(
            thread_id=thread_id,
            parent_task_id=meta.get("parent_task_id") or None,
            participants=[p for p in meta.get("participants", "").split(",") if p],
            messages=messages,
            created_at=created_at,
            session_id=meta.get("session_id", ""),
            kind=meta.get("kind", ""),
        )
