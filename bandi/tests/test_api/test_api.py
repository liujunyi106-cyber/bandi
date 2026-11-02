# 功能：FastAPI API 单元测试 — 健康检查 / 聊天 / 会话 / WebSocket

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(db_path))
    store.create_session("test-session-001", title="测试会话")
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


class TestHealth:
    def test_health(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestChat:
    def test_chat_basic(self) -> None:
        resp = client.post("/api/agent/chat", json={"message": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["reply"].startswith("Mock回复:")

    def test_chat_with_session(self) -> None:
        resp = client.post(
            "/api/agent/chat",
            json={
                "message": "第二轮",
                "session_id": "test-session-001",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "test-session-001"

    def test_chat_message_null_rejected(self) -> None:
        resp = client.post("/api/agent/chat", json={"message": None})
        assert resp.status_code == 400

    def test_chat_message_non_string_rejected(self) -> None:
        resp = client.post("/api/agent/chat", json={"message": 123})
        assert resp.status_code == 400

    def test_chat_unknown_session_returns_404(self) -> None:
        resp = client.post(
            "/api/agent/chat",
            json={"message": "hi", "session_id": "no-such-session"},
        )
        assert resp.status_code == 404

    def test_chat_user_message_stored_once(self) -> None:
        resp = client.post("/api/agent/chat", json={"message": "只存一次"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]
        sess = client.get(f"/api/sessions/{sid}")
        assert sess.status_code == 200
        entries = sess.json()["messages"]
        user_msgs = [m for m in entries if m.get("role") == "user"]
        assert len(user_msgs) == 1


class TestSessions:
    def test_list_sessions(self) -> None:
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert isinstance(sessions, list)
        assert len(sessions) >= 1

    def test_get_session(self) -> None:
        resp = client.get("/api/sessions/test-session-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"]["id"] == "test-session-001"

    def test_get_nonexistent_session(self) -> None:
        resp = client.get("/api/sessions/nonexistent")
        assert resp.status_code == 404

    def test_delete_session(self) -> None:
        resp = client.delete("/api/sessions/test-session-001")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_nonexistent(self) -> None:
        resp = client.delete("/api/sessions/nonexistent")
        assert resp.status_code == 404

    def test_explain_session_with_steps(self) -> None:
        from bandi.core.message import Message, MessageRole
        from bandi.memory.session.store import SessionStore

        store = SessionStore(str(Path.home() / ".bandi" / "data" / "bandi.db"))
        store.append_message(
            "test-session-001",
            Message(role=MessageRole.USER, content="查一下进度"),
        )
        store.append_message(
            "test-session-001",
            Message(
                role=MessageRole.ASSISTANT,
                content="已完成",
                tool_calls=[],
            ),
        )
        store.close()
        resp = client.post("/api/sessions/test-session-001/explain")
        assert resp.status_code == 200
        data = resp.json()
        assert "steps" in data and "explanation" in data
        assert any(s["action"] == "user_asked" for s in data["steps"])
        assert any(s["action"] == "llm_decided" for s in data["steps"])
        assert "用户提问" in data["explanation"]

    def test_explain_empty_session(self) -> None:
        resp = client.post("/api/sessions/test-session-001/explain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["explanation"] == "（无执行轨迹）"


class TestCompanyCollabDispatch:
    def test_collab_send_dm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_COMMS_FROM", "worker")
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("BANDI_COMMS_PROJECT", "p1")
        monkeypatch.setenv("BANDI_COMMS_SESSION", "s1")
        resp = client.post(
            "/api/company/collab/send_dm",
            json={"to_agent": "bob", "subject": "hi", "body": "hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("is_error") is True
        assert "permission denied" in data.get("result", {}).get("error", "").lower()

    def test_collab_unknown_tool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_COMMS_FROM", "worker")
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("BANDI_COMMS_PROJECT", "p1")
        monkeypatch.setenv("BANDI_COMMS_SESSION", "s1")
        resp = client.post("/api/company/collab/nonexistent", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("is_error") is True

    def test_collab_no_role_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BANDI_COMMS_FROM", raising=False)
        resp = client.post("/api/company/collab/send_dm", json={"to_agent": "bob"})
        assert resp.status_code == 400
