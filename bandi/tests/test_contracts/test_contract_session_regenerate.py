from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import bandi.api.app  # noqa: F401 — 确保模块已加载（包属性 app 是 FastAPI 实例）
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


def _module() -> object:
    return sys.modules["bandi.api.app"]


@pytest.fixture
def store(tmp_path):
    value = SessionStore(str(tmp_path / "s.db"))
    value.create_session(session_id="s1", mode="execute")
    yield value
    value.close()


def _seed(store: SessionStore) -> tuple[str, str, str, str]:
    first_user = store.append_message("s1", Message(role=MessageRole.USER, content="第一问"))
    first_assistant = store.append_message(
        "s1", Message(role=MessageRole.ASSISTANT, content="第一答")
    )
    second_user_msg = Message(role=MessageRole.USER, content="第二问")
    second_user_msg.meta["attachments"] = [{"name": "brief.md", "path": "/tmp/brief.md"}]
    second_user = store.append_message("s1", second_user_msg)
    second_assistant = store.append_message(
        "s1", Message(role=MessageRole.ASSISTANT, content="第二答")
    )
    return first_user, first_assistant, second_user, second_assistant


def test_regenerate_keeps_user_and_drops_later_entries(monkeypatch, store) -> None:
    _, _, second_user, second_assistant = _seed(store)
    captured: dict[str, object] = {}

    async def fake_run_agent_turn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            reply="新的第二答",
            session_id="s1",
            terminal_reason="COMPLETED",
            total_tokens=1,
            turn_index=2,
        )

    deleted: list[str] = []

    class _FakeCheckpoints:
        def delete_by_session(self, session_id: str) -> None:
            deleted.append(session_id)

        def close(self) -> None:
            return None

    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr("bandi.agent.checkpoint.CheckpointStore", _FakeCheckpoints)
    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)

    result = asyncio.run(module.session_regenerate("s1", {"entry_id": second_assistant}))

    assert result["reply"] == "新的第二答"
    entries = store.get_full_branch("s1")
    assert [entry["id"] for entry in entries][-1] == second_user
    assert second_assistant not in [entry["id"] for entry in entries]
    assert captured["append_user_message"] is False
    assert captured["user_input"] == "第二问"
    assert captured["attachments"] == [{"name": "brief.md", "path": "/tmp/brief.md"}]
    assert deleted == ["s1"]


def test_regenerate_busy_does_not_truncate(monkeypatch, store) -> None:
    _, _, _, second_assistant = _seed(store)
    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)
    monkeypatch.setattr("bandi.core.agent_runner.session_slot_busy", lambda *_: True)

    with pytest.raises(HTTPException) as error:
        asyncio.run(module.session_regenerate("s1", {"entry_id": second_assistant}))

    assert error.value.status_code == 409
    assert [entry["id"] for entry in store.get_full_branch("s1")][-1] == second_assistant


def test_regenerate_exception_restores_history(monkeypatch, store) -> None:
    _, _, _, second_assistant = _seed(store)

    async def boom(**_kwargs):
        raise RuntimeError("provider down")

    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)
    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", boom)
    monkeypatch.setattr("bandi.core.agent_runner.session_slot_busy", lambda *_: False)

    class _FakeCheckpoints:
        def delete_by_session(self, session_id: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("bandi.agent.checkpoint.CheckpointStore", _FakeCheckpoints)

    with pytest.raises(RuntimeError):
        asyncio.run(module.session_regenerate("s1", {"entry_id": second_assistant}))

    assert [entry["id"] for entry in store.get_full_branch("s1")][-1] == second_assistant


def test_regenerate_rejects_assistant_without_prior_user(monkeypatch, store) -> None:
    assistant = store.append_message("s1", Message(role=MessageRole.ASSISTANT, content="孤立"))

    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)

    with pytest.raises(HTTPException) as error:
        asyncio.run(module.session_regenerate("s1", {"entry_id": assistant}))

    assert error.value.status_code == 400


def test_regenerate_rejects_entry_from_other_session(monkeypatch, store) -> None:
    store.create_session(session_id="s2", mode="execute")
    other = store.append_message("s2", Message(role=MessageRole.USER, content="别的会话"))
    store.append_message("s2", Message(role=MessageRole.ASSISTANT, content="别的回复"))

    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)

    with pytest.raises(HTTPException) as error:
        asyncio.run(module.session_regenerate("s1", {"entry_id": other}))

    assert error.value.status_code == 404
