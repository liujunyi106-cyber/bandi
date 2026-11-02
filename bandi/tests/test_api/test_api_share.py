from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import _share_lock, _share_tokens, app
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(db_path))
    store.create_session("test-session-001", title="test")
    store.close()
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", db_path),
        patch("bandi.api.app._store", None),
    ):
        yield
    with _share_lock:
        _share_tokens.clear()


class TestSessionShare:
    def test_share_creates_token(self):
        sid = "test-session-001"
        resp = client.post(f"/api/sessions/{sid}/share")
        assert resp.status_code == 200
        data = resp.json()
        assert "share_token" in data
        assert len(data["share_token"]) == 16

    def test_share_not_found(self):
        resp = client.post("/api/sessions/nonexistent/share")
        assert resp.status_code == 404

    def test_view_shared_session(self):
        sid = "test-session-001"
        from bandi.api.app import _get_store

        store = _get_store()
        store.append_message(sid, Message(role=MessageRole.USER, content="hello"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="hi"))
        share_resp = client.post(f"/api/sessions/{sid}/share")
        token = share_resp.json()["share_token"]
        view_resp = client.get(f"/api/shared/{token}")
        assert view_resp.status_code == 200
        data = view_resp.json()
        assert data["session_id"] == sid
        assert len(data["messages"]) == 2

    def test_view_invalid_token(self):
        resp = client.get("/api/shared/invalidtoken123456")
        assert resp.status_code == 404

    def test_view_shared_without_auth(self):
        sid = "test-session-001"
        from bandi.api.app import _get_store

        store = _get_store()
        store.append_message(sid, Message(role=MessageRole.USER, content="hello"))
        share_resp = client.post(f"/api/sessions/{sid}/share")
        token = share_resp.json()["share_token"]
        anonymous = TestClient(app)
        anonymous.headers.clear()
        view_resp = anonymous.get(f"/api/shared/{token}")
        assert view_resp.status_code == 200
        assert view_resp.json()["session_id"] == sid


class TestUserInputInject:
    def test_inject_missing_message(self):
        resp = client.post("/api/sessions/s1/inject", json={})
        assert resp.status_code == 400

    def test_inject_no_active_run(self):
        resp = client.post("/api/sessions/s1/inject", json={"message": "hello"})
        assert resp.status_code == 409


class TestCostDetail:
    def test_cost_detail_no_tracker(self):
        resp = client.get("/api/sessions/s1/cost-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tool_stats"] == {}
