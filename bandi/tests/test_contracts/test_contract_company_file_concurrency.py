# 功能：契约——公司侧文件写并发加固（tmp 名唯一化 + append-only 消息跨进程 union 合并）。
# 钉行为：原子写临时文件名唯一（并发写不共用同一 tmp 路径）；
# 线程/会议消息在"另一进程已写、本进程基于旧快照再写"时不得丢失任一方
# （append-only 语义下按 message_id union 合并，盘上为基底保序）。
from __future__ import annotations

import os
from pathlib import Path

from bandi.company.comms import _atomic_write_text
from bandi.company.communication import CommunicationManager, DirectMessage, MeetingRoom
from bandi.company.discussion_thread import ThreadManager

# ── 原子写 tmp 名唯一化 ──────────────────────────────────────────────


def test_atomic_write_uses_unique_tmp_names(tmp_path: Path, monkeypatch) -> None:
    import bandi.company.comms as comms_mod

    seen: list[str] = []
    real_replace = os.replace

    def _spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(comms_mod.os, "replace", _spy)
    target = tmp_path / "note.md"
    _atomic_write_text(target, "第一次")
    _atomic_write_text(target, "第二次")

    assert len(seen) == 2 and seen[0] != seen[1], (
        "两次原子写的临时文件名必须不同（共享 tmp 路径会写交错损坏）"
    )
    assert target.read_text(encoding="utf-8") == "第二次"
    assert list(tmp_path.glob("*.tmp")) == [], "写完成后不得残留 tmp 文件"


# ── 线程消息跨进程 union 合并 ────────────────────────────────────────


def test_thread_messages_union_across_concurrent_writers(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    writer_a = ThreadManager(workspace=workspace, project_id="p", session_id="s")
    thread = writer_a.create_thread(parent_task_id="t1", participants=["user", "tech"])
    writer_a.add_message(thread.thread_id, "user", "第一条", message_id="m1")

    # 模拟另一进程：读同一线程（拿到 m1）后追加 m2
    writer_b = ThreadManager(workspace=workspace, project_id="p", session_id="s")
    writer_b.hydrate()
    writer_b.add_message(thread.thread_id, "tech", "第二条", message_id="m2")

    # 进程 A 基于旧快照（只有 m1）继续追加 m3 并落盘——不得覆盖丢掉 m2
    writer_a.add_message(thread.thread_id, "user", "第三条", message_id="m3")

    reloaded = ThreadManager(workspace=workspace, project_id="p", session_id="s")
    reloaded.hydrate()
    final = reloaded.get_thread(thread.thread_id)
    assert final is not None
    ids = [m.message_id for m in final.messages]
    assert set(ids) == {"m1", "m2", "m3"}, f"并发写不得丢消息（实际 {ids}）"
    assert ids[0] == "m1", "盘上已提交前缀必须为基底保序"


# ── 会议消息跨进程 union 合并 ────────────────────────────────────────


def test_meeting_room_transcripts_union_across_writers(tmp_path: Path) -> None:
    def _mgr() -> CommunicationManager:
        return CommunicationManager(
            workspace=str(tmp_path), project_id="p", session_id="s", org_engine=None
        )

    writer_a = _mgr()
    room = MeetingRoom(id="r1", participants=["tech"], agenda="评审")
    room.messages.append(DirectMessage(sender_role="tech", recipient_role="", content="first"))
    writer_a._persist_room(room)

    writer_b = _mgr()
    disk_b = writer_b._load_room_from_disk("r1")
    assert disk_b is not None
    disk_b.messages.append(DirectMessage(sender_role="ceo", recipient_role="", content="second"))
    writer_b._persist_room(disk_b)

    # 进程 A 基于旧快照追加 third 落盘——second 不得被覆盖
    room.messages.append(DirectMessage(sender_role="tech", recipient_role="", content="third"))
    writer_a._persist_room(room)

    final = writer_a._load_room_from_disk("r1")
    assert final is not None
    contents = [m.content for m in final.messages]
    assert contents == ["first", "second", "third"], f"会议消息并发写不得丢（实际 {contents}）"
