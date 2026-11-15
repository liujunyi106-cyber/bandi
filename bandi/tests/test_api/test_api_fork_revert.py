# 功能：Day127 Fork/Revert API 端点接线测试（生产装配点：api/app.py /api/sessions/{id}/fork + /revert/*）
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
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
    store.create_session("fork-src", title="src")
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


def _seed_entries(sid: str) -> list[str]:
    from bandi.api.app import _get_store

    store = _get_store()
    ids = []
    ids.append(store.append_message(sid, Message(role=MessageRole.USER, content="m0")))
    ids.append(store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="m1")))
    ids.append(store.append_message(sid, Message(role=MessageRole.USER, content="m2")))
    return ids


class TestForkApi:
    def test_fork_copies_prefix(self):
        ids = _seed_entries("fork-src")
        resp = client.post("/api/sessions/fork-src/fork", json={"entry_id": ids[1]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["forked_session_id"] != "fork-src"
        new_sid = data["forked_session_id"]

        from bandi.api.app import _get_store

        store = _get_store()
        branch = store.get_branch(new_sid)
        assert [e["content"] for e in branch if e.get("entry_type") == "message"] == ["m0", "m1"]
        assert len(store.get_branch("fork-src")) == 3

    def test_fork_unknown_entry_404(self):
        resp = client.post("/api/sessions/fork-src/fork", json={"entry_id": "nope"})
        assert resp.status_code == 404

    def test_fork_missing_entry_id_400(self):
        resp = client.post("/api/sessions/fork-src/fork", json={})
        assert resp.status_code == 400

    def test_fork_missing_session_404(self):
        resp = client.post("/api/sessions/nope/fork", json={"entry_id": "x"})
        assert resp.status_code == 404


class TestRevertApi:
    def test_stage_commit_flow(self):
        ids = _seed_entries("fork-src")
        stage = client.post("/api/sessions/fork-src/revert/stage", json={"entry_id": ids[1]})
        assert stage.status_code == 200
        assert stage.json()["staged"] is True

        commit = client.post("/api/sessions/fork-src/revert/commit")
        assert commit.status_code == 200
        assert commit.json()["removed_entries"] == 1

        from bandi.api.app import _get_store

        store = _get_store()
        assert [e["content"] for e in store.get_branch("fork-src")] == ["m0", "m1"]

    def test_stage_clear_keeps_data(self):
        ids = _seed_entries("fork-src")
        assert (
            client.post(
                "/api/sessions/fork-src/revert/stage", json={"entry_id": ids[0]}
            ).status_code
            == 200
        )
        clear = client.post("/api/sessions/fork-src/revert/clear")
        assert clear.status_code == 200

        from bandi.api.app import _get_store

        store = _get_store()
        assert len(store.get_branch("fork-src")) == 3

    def test_commit_without_stage_conflict(self):
        resp = client.post("/api/sessions/fork-src/revert/commit")
        assert resp.status_code == 409

    def test_stage_unknown_entry_404(self):
        resp = client.post("/api/sessions/fork-src/revert/stage", json={"entry_id": "nope"})
        assert resp.status_code == 404

    def test_revert_missing_session_404(self):
        assert client.post("/api/sessions/nope/revert/commit").status_code == 404


# ── 无状态预览 ?at 端到端 ──────────────────────────────────────────────


class TestResolvedBranchPreview:
    def test_get_session_at_full(self):
        _seed_entries("fork-src")
        resp = client.get("/api/sessions/fork-src")
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 3

    def test_get_session_at_truncates_inclusive(self):
        ids = _seed_entries("fork-src")
        resp = client.get(f"/api/sessions/fork-src?at={ids[1]}")
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 2
        assert msgs[-1]["id"] == ids[1], "截断应含恢复点自身"

    def test_get_session_at_unknown_404(self):
        _seed_entries("fork-src")
        resp = client.get("/api/sessions/fork-src?at=nonexistent")
        assert resp.status_code == 404

    def test_explain_at_truncates(self):
        ids = _seed_entries("fork-src")
        resp = client.post(f"/api/sessions/fork-src/explain?at={ids[0]}")
        assert resp.status_code == 200
        assert len(resp.json()["steps"]) >= 1
