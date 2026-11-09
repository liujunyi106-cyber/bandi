# 功能：API 流式端点测试 — SSE / session 持久化 / 模型设置

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(db_path))
    store.create_session("test-session-001", title="test")
    store.close()
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.llm.default_model = "mock"
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", db_path),
        patch("bandi.api.app._store", None),
        patch("bandi.api.app._ontology_store", None),
    ):
        yield


class TestAgentStream:
    def test_stream_endpoint_returns_sse(self) -> None:
        resp = client.post("/api/agent/stream", json={"message": "hello"})
        assert resp.status_code == 200
        assert "event:" in resp.text

    def test_stream_endpoint_error_frame_on_exception(self) -> None:
        """run_agent_turn 抛异常时 SSE 必须发 error 帧收尾，不得永久挂起。"""

        async def _boom(**kwargs):
            raise RuntimeError("provider exploded")

        with patch("bandi.core.agent_runner.run_agent_turn", side_effect=_boom):
            resp = client.post("/api/agent/stream", json={"message": "hello"})
        assert resp.status_code == 200
        assert "event: error" in resp.text
        assert "provider exploded" in resp.text

    def test_plan_endpoint(self) -> None:
        resp = client.post("/api/agent/plan", json={"message": "plan this"})
        assert resp.status_code == 200
        assert "reply" in resp.json()

    def test_model_settings_api(self) -> None:
        resp = client.get("/api/settings/model")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_model" in data
        assert "has_api_key" in data
