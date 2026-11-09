# 功能：会话模式切换契约——落库到 sessions.mode、非法值拒绝、execute 释放 plan 门

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import bandi.api.app  # noqa: F401 — 确保模块加载（`bandi.api.app` 属性被 FastAPI 实例遮蔽）
from bandi.memory.session.store import SessionStore


def _module() -> object:
    """功能：取 app 模块本体（`bandi.api.app` 属性名被 FastAPI 实例遮蔽）。"""
    return sys.modules["bandi.api.app"]


def _call(body: dict[str, object]) -> dict[str, object]:
    return asyncio.run(_module().company_chat_mode(body))


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, store: SessionStore) -> SessionStore:
    module = _module()
    monkeypatch.setattr(module, "_get_store", lambda: store)
    monkeypatch.setattr(
        module,
        "_get_company_dispatcher",
        lambda: SimpleNamespace(
            store=SimpleNamespace(list_all=lambda: []), signal_dispatcher_wake=lambda: None
        ),
    )
    return store


class TestModePersist:
    def test_plan_mode_persisted_to_session(self, patched: SessionStore) -> None:
        sid = patched.create_session(mode="execute")
        out = _call({"mode": "plan", "session_id": sid})
        assert out["mode"] == "plan"
        assert patched.get_session(sid)["mode"] == "plan"

    def test_execute_mode_persisted_after_plan(self, patched: SessionStore) -> None:
        sid = patched.create_session(mode="plan")
        _call({"mode": "execute", "session_id": sid})
        assert patched.get_session(sid)["mode"] == "execute"

    def test_persisted_visible_through_session_detail(self, patched: SessionStore) -> None:
        """详情端点返回 SELECT *，mode 列天然下发——切换后即可读到。"""
        sid = patched.create_session(mode="execute")
        _call({"mode": "plan", "session_id": sid})
        detail = asyncio.run(_module().get_session(sid))
        assert detail["session"]["mode"] == "plan"


class TestModeValidation:
    def test_invalid_mode_rejected(self, patched: SessionStore) -> None:
        sid = patched.create_session(mode="execute")
        with pytest.raises(HTTPException) as exc:
            _call({"mode": "ask", "session_id": sid})
        assert exc.value.status_code == 400
        assert patched.get_session(sid)["mode"] == "execute"

    def test_missing_session_does_not_create_row(self, patched: SessionStore) -> None:
        out = _call({"mode": "plan", "session_id": "no-such-session"})
        assert out["mode"] == "plan"
        assert patched.get_session("no-such-session") is None

    def test_blank_session_id_is_noop(self, patched: SessionStore) -> None:
        out = _call({"mode": "plan"})
        assert out == {"released": 0, "mode": "plan"}


class TestGateReleaseUnchanged:
    def test_execute_without_gated_items_releases_zero(self, patched: SessionStore) -> None:
        sid = patched.create_session(mode="plan")
        out = _call({"mode": "execute", "session_id": sid})
        assert out["released"] == 0
        assert out["mode"] == "execute"
