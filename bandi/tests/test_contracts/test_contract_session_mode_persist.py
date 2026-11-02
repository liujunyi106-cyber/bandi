# 功能：契约——会话级 mode 持久化（审计：API 每轮 mode 取 body
# 缺省 execute——plan 会话恢复后 mode 丢失）。
from __future__ import annotations

import pytest

from bandi.core.agent_runner import AgentTurnResult
from bandi.memory.session.store import SessionStore


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> tuple:
    import importlib

    app_mod = importlib.import_module("bandi.api.app")
    store = SessionStore(str(tmp_path / "sess.db"))
    calls: dict = {}

    async def _fake_turn(**kwargs):
        calls.update(kwargs)
        return AgentTurnResult(
            reply="ok",
            terminal_reason="COMPLETED",
            total_tokens=0,
            turn_index=1,
            session_id=kwargs.get("session_id") or "s1",
        )

    monkeypatch.setattr(app_mod, "_get_store", lambda: store)
    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", _fake_turn)
    return app_mod, store, calls


@pytest.mark.asyncio
async def test_chat_inherits_session_mode_when_body_omits(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """契约（核心）：body 未传 mode 时继承会话上次 mode（恢复不丢失）。"""
    app_mod, store, calls = _patch(monkeypatch, tmp_path)
    sid = store.create_session(mode="plan")

    result = await app_mod.chat({"message": "继续", "session_id": sid})

    assert calls.get("mode") == "plan", f"必须继承会话 mode（实际 {calls.get('mode')}）"
    assert result["mode"] == "plan"


@pytest.mark.asyncio
async def test_chat_explicit_mode_persists_and_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """契约：body 显式传 mode 时以 body 为准并写回会话（切换持久化）。"""
    app_mod, store, calls = _patch(monkeypatch, tmp_path)
    sid = store.create_session(mode="plan")

    await app_mod.chat({"message": "切执行", "session_id": sid, "mode": "execute"})

    assert calls.get("mode") == "execute"
    assert store.get_session(sid)["mode"] == "execute", "显式切换必须写回会话"


@pytest.mark.asyncio
async def test_chat_new_session_uses_body_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """契约（对照）：新会话（未带 session_id）用 body mode 且持久化（缺省 execute）。"""
    app_mod, store, calls = _patch(monkeypatch, tmp_path)

    await app_mod.chat({"message": "新会话", "mode": "plan"})

    assert calls.get("mode") == "plan"
    created = store.get_session(calls["session_id"])
    assert created is not None and created["mode"] == "plan"
