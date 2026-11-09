# 功能：通信信道——消息收发 + 信箱 + 未读跟踪

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.company.handoff import AgentHandoff

COMMS_ROOT_NAME = ".bandi-comms"
INBOX_DIRNAME = "inbox"
MEETINGS_DIRNAME = "meetings"
THREADS_DIRNAME = "threads"
NEW_DIRNAME = "new"
SEEN_DIRNAME = "seen"
OUTBOX_DIRNAME = "outbox"
TMP_DIRNAME = ".tmp"


# 在 Windows 直接 OSError。白名单正则（skills/path_guard）已在项目/技能入口挡住它们
# **同族第二入口**，必须享受同一清单。
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_path_segment(value: str, field_name: str) -> str:
    """校验路径段：拒绝 空/`.`/`..`/分隔符/盘符/Windows 保留名/首尾空格与点（fail-closed）。

    为什么连保留名与尾随点也拒（清单）：Windows 上 `mkdir("CON")` 直接 OSError；
    而 NTFS 把 `foo.` 与 `foo` 视为同一名字 ⇒ 两个不同 id 会**静默撞同一目录**
    比报错更危险，故**拒绝而非静默 strip**。
    """
    raw_value = str(value or "")
    text = raw_value.strip()
    if not text or text in {".", ".."}:
        raise ValueError(f"{field_name} must be a non-empty path segment, got {value!r}.")
    if "/" in text or chr(92) in text or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{field_name} must not contain path separators, got {value!r}.")
    if text != raw_value or text != text.rstrip(" ."):
        raise ValueError(
            f"{field_name} must not have leading/trailing spaces or dots, got {value!r}."
        )
    if text.split(".")[0].upper() in _WINDOWS_RESERVED_STEMS:
        raise ValueError(f"{field_name} must not be a Windows reserved device name, got {value!r}.")
    return text


def _comms_root(workspace: str, project_id: str, session_id: str) -> Path:
    safe_project = _safe_path_segment(project_id, "project_id")
    safe_session = _safe_path_segment(session_id, "session_id")
    return Path(workspace) / COMMS_ROOT_NAME / safe_project / safe_session


def _inbox_dir(root: Path, role_id: str) -> Path:
    return root / INBOX_DIRNAME / _safe_path_segment(role_id, "role_id")


def _ensure_layout(root: Path, role_ids: list[str]) -> None:
    for role_id in role_ids:
        for d in (NEW_DIRNAME, SEEN_DIRNAME, OUTBOX_DIRNAME, TMP_DIRNAME):
            (_inbox_dir(root, role_id) / d).mkdir(parents=True, exist_ok=True)


def _message_file_exists(root: Path, recipient_role: str, msg_id: str) -> bool:
    """文件级幂等：收件人 NEW 或 sender OUTBOX 已有同 id 消息文件即视为已发送。"""
    inbox = _inbox_dir(root, recipient_role)
    return (inbox / NEW_DIRNAME / f"{msg_id}.md").exists() or (
        inbox / OUTBOX_DIRNAME / f"{msg_id}.md"
    ).exists()


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写：先写同目录唯一 .tmp 再 os.replace，避免半写入与并发写共用 tmp 交错。"""
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def _atomic_append_text(path: Path, text: str) -> None:
    """原子追加：读原内容 + 追加后整体原子写回。"""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic_write_text(path, existing + text)


@dataclass
class CommsMessage:
    id: str
    sender_role: str
    recipient_role: str
    subject: str
    body: str
    blocking: bool
    metadata: dict[str, Any]
    created_at: datetime
    read: bool = False

    @property
    def is_read(self) -> bool:
        return self.read

    @property
    def handoff(self) -> AgentHandoff | None:
        """功能：结构化交接契约回放（frontmatter `handoff` 单行 JSON）——
        缺失/损坏返回 None（向后兼容纯文本消息）。"""
        return AgentHandoff.from_json(self.metadata.get("handoff"))

    @classmethod
    def from_file(cls, path: Path, read: bool = False) -> CommsMessage:
        text = path.read_text(encoding="utf-8")
        front, _, body = text.partition("\n---\n")
        meta: dict[str, Any] = {}
        for line in front.strip().split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        try:
            created_at = (
                datetime.fromisoformat(meta["created_at"]) if "created_at" in meta else None
            )
        except ValueError:
            created_at = None
        return cls(
            id=meta.get("id", path.stem),
            sender_role=meta.get("sender_role", "unknown"),
            recipient_role=meta.get("recipient_role", "unknown"),
            subject=meta.get("subject", ""),
            body=body.strip(),
            blocking=meta.get("blocking", "false").lower() == "true",
            metadata=meta,
            created_at=created_at or datetime.now(UTC),
            read=read,
        )


def send_message(
    workspace: str,
    project_id: str,
    session_id: str,
    sender_role: str,
    recipient_role: str,
    subject: str,
    body: str,
    blocking: bool = False,
    extra_metadata: dict[str, str] | None = None,
    reply_to_msg_id: str | None = None,
    message_id: str | None = None,
    handoff: AgentHandoff | None = None,
) -> str:
    root = _comms_root(workspace, project_id, session_id)
    _ensure_layout(root, [sender_role, recipient_role])
    msg_id = message_id or str(uuid.uuid4())
    # 不覆盖重写（跨进程重启后 _sent_messages 清空，文件级检查兜底）。
    existing = _message_file_exists(root, recipient_role, msg_id)
    if existing:
        return msg_id
    now = datetime.now(UTC)
    lines = [
        f"id: {msg_id}",
        f"sender_role: {sender_role}",
        f"recipient_role: {recipient_role}",
        f"subject: {subject}",
        f"blocking: {'true' if blocking else 'false'}",
        f"created_at: {now.isoformat()}",
    ]
    if reply_to_msg_id:
        lines.append(f"reply_to_msg_id: {reply_to_msg_id}")
    if extra_metadata:
        for k, v in extra_metadata.items():
            lines.append(f"{k}: {v}")
    if handoff is not None:
        lines.append(f"handoff: {handoff.to_json()}")
    content = "\n".join(lines) + "\n---\n" + body
    tmp_dir = _inbox_dir(root, sender_role) / TMP_DIRNAME
    tmp_path = tmp_dir / f"{msg_id}.md"
    tmp_path.write_text(content, encoding="utf-8")
    dest_path = _inbox_dir(root, recipient_role) / NEW_DIRNAME / f"{msg_id}.md"
    shutil.move(str(tmp_path), str(dest_path))
    outbox_path = _inbox_dir(root, sender_role) / OUTBOX_DIRNAME / f"{msg_id}.md"
    shutil.copy2(str(dest_path), str(outbox_path))
    return msg_id


def list_inbox(
    workspace: str,
    project_id: str,
    session_id: str,
    role_id: str,
    include_seen: bool = False,
) -> list[CommsMessage]:
    root = _comms_root(workspace, project_id, session_id)
    new_dir = _inbox_dir(root, role_id) / NEW_DIRNAME
    messages: list[CommsMessage] = []
    if new_dir.exists():
        for p in sorted(new_dir.iterdir()):
            if p.suffix == ".md":
                messages.append(CommsMessage.from_file(p, read=False))
    if include_seen:
        seen_dir = _inbox_dir(root, role_id) / SEEN_DIRNAME
        if seen_dir.exists():
            for p in sorted(seen_dir.iterdir()):
                if p.suffix == ".md":
                    messages.append(CommsMessage.from_file(p, read=True))
    return messages


def mark_read(
    workspace: str,
    project_id: str,
    session_id: str,
    role_id: str,
    message_id: str,
) -> bool:
    _safe_path_segment(message_id, "message_id")
    root = _comms_root(workspace, project_id, session_id)
    new_dir = _inbox_dir(root, role_id) / NEW_DIRNAME
    src = new_dir / f"{message_id}.md"
    if not src.exists():
        return False
    seen_dir = _inbox_dir(root, role_id) / SEEN_DIRNAME
    shutil.move(str(src), str(seen_dir / f"{message_id}.md"))
    return True


def create_meeting(
    workspace: str,
    project_id: str,
    session_id: str,
    meeting_id: str,
    title: str,
    participants: list[str],
) -> Path:
    root = _comms_root(workspace, project_id, session_id)
    meeting_dir = root / MEETINGS_DIRNAME / _safe_path_segment(meeting_id, "meeting_id")
    meeting_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "meeting_id": meeting_id,
        "title": title,
        "participants": participants,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    lines = [
        f"{k}: {v}" if not isinstance(v, list) else f"{k}: {','.join(v)}"
        for k, v in manifest.items()
    ]
    _atomic_write_text(meeting_dir / "manifest.yaml", "\n".join(lines))
    _atomic_write_text(meeting_dir / "transcript.md", f"# {title}\n\n")
    return meeting_dir


def append_transcript(
    workspace: str,
    project_id: str,
    session_id: str,
    meeting_id: str,
    speaker: str,
    text: str,
) -> None:
    root = _comms_root(workspace, project_id, session_id)
    transcript_path = (
        root / MEETINGS_DIRNAME / _safe_path_segment(meeting_id, "meeting_id") / "transcript.md"
    )
    if not transcript_path.exists():
        return
    _atomic_append_text(transcript_path, f"\n**{speaker}:** {text}\n")
