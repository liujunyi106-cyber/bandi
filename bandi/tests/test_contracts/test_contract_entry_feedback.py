# 功能：契约——消息点赞落库与记忆提名（PATCH /api/sessions/{id}/entries/{entry_id}）。
# 钉行为：up 落库 session_entries.feedback / 取消点赞落 NULL / 点赞提名进待批准队列
# （subsystem=memory、action=store_long_term、origin_source=user_like）/ 重复点赞幂等 /
# 破坏者：entry 不属该会话 404、非法值 400、无内容不提名、提名失败不影响点赞本身。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.memory.staging import reset_pending_store

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    reset_pending_store()
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield
    reset_pending_store()


def _seed(tmp_path: Path, *, content: str = "手冲定价 28 元，会员第二杯半价。") -> str:
    """功能：真实 sqlite 建表并写入会话与一条 assistant 回复（返回 entry_id）。"""
    import sqlite3

    from bandi.memory.session.store import SessionStore

    db = tmp_path / ".bandi" / "data" / "bandi.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SessionStore(str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (id, title, status, created_at, updated_at, metadata)"
        " VALUES ('sess-fb','点赞会话','active',datetime('now'),datetime('now'),'{}')"
    )
    conn.execute(
        "INSERT INTO session_entries (id, session_id, entry_type, role, content, created_at)"
        " VALUES ('ent-fb-1','sess-fb','message','assistant',?,datetime('now'))",
        (content,),
    )
    conn.commit()
    conn.close()
    return "ent-fb-1"


def _stored_feedback(tmp_path: Path, entry_id: str = "ent-fb-1") -> str | None:
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / ".bandi" / "data" / "bandi.db"))
    row = conn.execute("SELECT feedback FROM session_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return row[0] if row else None


class TestLikePersist:
    def test_up_is_persisted_in_db(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        resp = client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["feedback"] == "up"
        assert _stored_feedback(tmp_path) == "up", "点赞必须真正落库（刷新后不丢）"

    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"})
        resp = client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": None})
        assert resp.status_code == 200, resp.text
        assert _stored_feedback(tmp_path) is None, "取消点赞必须落 NULL 而不是空串"

    def test_like_nominates_pending_memory(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"})
        pending = client.get("/api/memory/pending").json()["pending"]
        liked = [r for r in pending if r["action"] == "store_long_term"]
        assert liked, "点赞必须提名一条待批准记忆（人工闸门）"
        detail = client.get("/api/memory/pending").json()["pending"]
        assert detail[0]["subsystem"] == "memory", "提名项必须归入记忆子系统"
        assert "28" in detail[0]["summary"], "摘要应来自被点赞的回复正文"

    def test_repeat_like_is_idempotent(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        for _ in range(3):
            client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"})
        pending = client.get("/api/memory/pending").json()["pending"]
        assert len(pending) == 1, f"重复点赞不得重复提名（实得 {len(pending)} 条）"

    def test_blank_content_is_not_nominated(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path, content="   ")
        resp = client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"})
        assert resp.status_code == 200
        assert resp.json()["nominated"] is False, "空内容不得提名"
        assert client.get("/api/memory/pending").json()["pending"] == []

    def test_nomination_failure_keeps_like(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        with patch(
            "bandi.memory.staging.MemoryPendingStore.stage", side_effect=RuntimeError("disk")
        ):
            resp = client.patch(
                f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "up"}
            )
        assert resp.status_code == 200, "提名失败不得让点赞请求报错"
        assert resp.json()["nominated"] is False
        assert _stored_feedback(tmp_path) == "up", "点赞是既成事实，不得因提名失败回滚"


class TestLikeRejections:
    def test_entry_from_other_session_404(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        resp = client.patch("/api/sessions/other-session/entries/ent-fb-1", json={"feedback": "up"})
        assert resp.status_code == 404
        assert resp.json()["message"] == "SESSION_NOT_FOUND"

    def test_unknown_entry_404(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        resp = client.patch("/api/sessions/sess-fb/entries/ghost", json={"feedback": "up"})
        assert resp.status_code == 404
        assert resp.json()["message"] == "ENTRY_NOT_FOUND"

    def test_invalid_value_400(self, tmp_path: Path) -> None:
        entry_id = _seed(tmp_path)
        resp = client.patch(f"/api/sessions/sess-fb/entries/{entry_id}", json={"feedback": "down"})
        assert resp.status_code == 400, "仅接受 up 或空值清除"
        assert resp.json()["message"] == "INVALID_FEEDBACK_VALUE"
        assert _stored_feedback(tmp_path) is None, "非法值不得落库"
