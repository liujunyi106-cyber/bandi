# 功能：契约——api/session 子批 7 项修复（第 4 轮审计，收官）
# 钉行为（不钉实现）：
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


# ── abort 条件注销 ──────────────────────────────────────────────────


class TestAbortUnregisterConditional:
    def test_old_turn_finally_keeps_replaced_controller(self) -> None:
        import sys

        app_mod = sys.modules["bandi.api.app"]

        old_ctrl = object()
        new_ctrl = object()
        app_mod._active_abort_controllers["s-b1"] = old_ctrl
        app_mod._unregister_abort_controller("s-b1", old_ctrl)
        assert app_mod._active_abort_controllers.get("s-b1") is None

        # 场景：新轮已替换注册，旧轮 finally 收尾
        app_mod._active_abort_controllers["s-b1"] = new_ctrl
        app_mod._unregister_abort_controller("s-b1", old_ctrl)
        assert app_mod._active_abort_controllers.get("s-b1") is new_ctrl, (
            "旧轮注销不得弹掉新轮已替换的 controller（否则新轮无法 abort）"
        )
        app_mod._unregister_abort_controller("s-b1", new_ctrl)
        assert app_mod._active_abort_controllers.get("s-b1") is None


# ── SESSION_BUSY 不静默 ─────────────────────────────────────────────


@pytest.fixture()
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys

    app_mod = sys.modules["bandi.api.app"]
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    monkeypatch.setattr(app_mod, "_store", store)
    with patch("bandi.api.app._API_KEY", "test"):
        yield store, sid
    store.close()


class TestSessionBusyNotSilent:
    def test_chat_returns_409_and_writes_nothing(self, api_store) -> None:
        from bandi.core.agent_runner import _acquire_session_slot, _release_session_slot

        store, sid = api_store
        assert _acquire_session_slot(sid, store=store) is True
        try:
            resp = client.post(
                "/api/agent/chat", json={"message": "并发第二请求", "session_id": sid}
            )
            assert resp.status_code == 409, (
                f"SESSION_BUSY 必须 409（不静默 200）: {resp.status_code}"
            )
            assert "SESSION_BUSY" in resp.text
            assert store.get_branch(sid) == [], "被拒请求必须零写入（不落幽灵用户消息）"
        finally:
            _release_session_slot(sid, store=store)

    def test_stream_returns_409(self, api_store) -> None:
        from bandi.core.agent_runner import _acquire_session_slot, _release_session_slot

        store, sid = api_store
        assert _acquire_session_slot(sid, store=store) is True
        try:
            resp = client.post("/api/agent/stream", json={"message": "并发", "session_id": sid})
            assert resp.status_code == 409, f"流式端点 SESSION_BUSY 必须 409: {resp.status_code}"
        finally:
            _release_session_slot(sid, store=store)


# ── 最新 leaf 按插入序 ──────────────────────────────────────────────


class TestLatestLeafInsertionOrder:
    def test_same_timestamp_picks_last_inserted(self, tmp_path: Path) -> None:
        from bandi.memory.session.store import SessionStore

        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        ts = "2026-09-21T10:00:00.000000"
        # 先插入 id 字典序更大的（zzz），后插入更小（aaa）——同 created_at
        for entry_id, content in (("zzz-first", "first"), ("aaa-second", "second")):
            store._sqlite.execute(
                "INSERT INTO session_entries (id, session_id, parent_id, entry_type, "
                "turn_index, role, content, tool_call_id, metadata, created_at) "
                "VALUES (?, ?, NULL, 'message', 0, 'user', ?, NULL, NULL, ?)",
                (entry_id, sid, content, ts),
            )
        store._sqlite.commit()
        leaf = store._get_latest_leaf(sid)
        assert leaf == "aaa-second", (
            f"同刻必须按插入序取最新（uuid 字典序随机会给错 parent），实际 {leaf!r}"
        )
        store.close()


# ── revert 与运行中 turn 互斥 ───────────────────────────────────────


class TestRevertMutex:
    def test_revert_commit_409_when_session_busy(self, api_store) -> None:
        store, sid = api_store
        with patch("bandi.core.agent_runner.session_slot_busy", return_value=True):
            resp = client.post(f"/api/sessions/{sid}/revert/commit")
        assert resp.status_code == 409, f"运行中会话 revert/commit 必须 409: {resp.status_code}"
        assert "SESSION_BUSY" in resp.text

    def test_revert_stage_409_when_session_busy(self, api_store) -> None:
        store, sid = api_store
        with patch("bandi.core.agent_runner.session_slot_busy", return_value=True):
            resp = client.post(f"/api/sessions/{sid}/revert/stage", json={"entry_id": "x"})
        assert resp.status_code == 409

    def test_revert_undo_restores_committed_truncation(self, api_store) -> None:
        from bandi.core.message import Message, MessageRole

        store, sid = api_store
        first = store.append_message(sid, Message(role=MessageRole.USER, content="第一条"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="第二条"))
        staged = client.post(f"/api/sessions/{sid}/revert/stage", json={"entry_id": first})
        assert staged.status_code == 200
        committed = client.post(f"/api/sessions/{sid}/revert/commit")
        assert committed.status_code == 200
        assert committed.json()["operation_id"]
        assert len(store.get_branch(sid)) == 1

        undone = client.post(f"/api/sessions/{sid}/revert/undo")
        assert undone.status_code == 200
        assert undone.json()["restored_entries"] == 1
        assert len(store.get_branch(sid)) == 2

    def test_revert_undo_409_when_session_busy(self, api_store) -> None:
        store, sid = api_store
        with patch("bandi.core.agent_runner.session_slot_busy", return_value=True):
            resp = client.post(f"/api/sessions/{sid}/revert/undo")
        assert resp.status_code == 409


# ── WS 错误帧 schema ────────────────────────────────────────────────


class TestWsErrorSchema:
    def test_invalid_json_frame_is_object_payload(self) -> None:
        with patch("bandi.api.app._API_KEY", "test"):
            with client.websocket_connect(
                "/api/agent/stream", headers={"Authorization": "Bearer test"}
            ) as ws:
                ws.send_text("not-json{")
                frame = ws.receive_json()
        assert frame.get("type") == "error", f"必须回 error 帧: {frame}"
        assert isinstance(frame.get("data"), dict), (
            f"错误帧 data 必须为对象（schema 统一，前端按 dict 解析）: {frame.get('data')!r}"
        )
        assert "invalid JSON" in str(frame["data"].get("message", ""))


# ── EventBus 轮转失败不停写 ─────────────────────────────────────────


class TestEventBusRotateFailure:
    def test_rotation_failure_keeps_persisting(self, tmp_path: Path, monkeypatch) -> None:
        from bandi.core.event_bus import EventBus
        from bandi.core.events import Event, EventType

        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        asyncio.run(bus.emit(Event(type=EventType.LLM_STREAM_EVENT, data={"n": 1})))

        real_rename = Path.rename

        def failing_rename(self: Path, target: Any) -> None:
            if str(target).endswith(".jsonl.1"):
                raise OSError("locked by another process")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", failing_rename)
        from bandi.core import event_bus as eb_mod

        bus._written_bytes = eb_mod._PERSIST_ROTATE_BYTES + 1  # 触发下次写入轮转
        asyncio.run(bus.emit(Event(type=EventType.LLM_STREAM_EVENT, data={"n": 2})))

        bus.disable_persistence()  # join worker + flush/close：落盘完成后断言最终产物
        content = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert '"n": 2' in content, "轮转失败后必须继续写原文件（不得静默停写）"


# ── collab rooms 原子写 ─────────────────────────────────────────────


class TestCollabAtomicWrite:
    def test_failed_replace_keeps_previous_file(self, tmp_path: Path, monkeypatch) -> None:
        from bandi.cli import collab
        from bandi.company.communication import CommunicationManager, MeetingRoom

        comms = CommunicationManager(str(tmp_path / "ws"), "p", "s")
        path = collab._meetings_path(comms)
        room = MeetingRoom(id="r1", participants=["a"], agenda="topic")
        collab._save_room(comms, room)
        assert json.loads(path.read_text(encoding="utf-8"))["r1"]["id"] == "r1"

        room.outcome = "changed-after"
        real_replace = collab.os.replace

        def failing_replace(src: Any, dst: Any) -> None:
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(collab.os, "replace", failing_replace)
        with pytest.raises(OSError):
            collab._save_room(comms, room)
        monkeypatch.setattr(collab.os, "replace", real_replace)
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert disk["r1"]["outcome"] == "", "中途失败不得损坏/半写原文件（原子替换语义）"
