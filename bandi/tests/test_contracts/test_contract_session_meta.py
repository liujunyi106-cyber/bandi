# 功能：契约——会话元数据扩展（session project_id 维度 + PATCH rename）。
# 钉行为不钉实现：create_session 带 project_id 落 metadata（无则 "{}" 旧格式）/
# 不存在 404 / DoD-12 端到端（建会话→改名→列表同回 project_id 与 title）。
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.memory.session.store import SessionStore

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", db_path),
        patch("bandi.api.app._store", None),
    ):
        yield


class TestSessionProjectMeta:
    def test_store_create_session_records_title(self, tmp_path: Path) -> None:
        """create_session 支持 title（新会话标题创建时一次写入，无 PATCH 竞态）。"""
        store = SessionStore(str(tmp_path / ".bandi" / "data" / "bandi.db"))
        sid = store.create_session(title="首条消息作标题")
        assert store.get_session(sid)["title"] == "首条消息作标题"
        store.close()

    def test_store_create_session_records_project_id(self, tmp_path: Path) -> None:
        store = SessionStore(str(tmp_path / ".bandi" / "data" / "bandi.db"))
        sid = store.create_session(project_id="p-alpha", project_root="D:/work")
        session = store.get_session(sid)
        assert session is not None
        meta = json.loads(session["metadata"])
        assert meta["project_id"] == "p-alpha"
        assert meta["project_root"] == "D:/work"

        plain = store.create_session()
        plain_meta = json.loads(store.get_session(plain)["metadata"])
        assert "project_id" not in plain_meta, "无项目会话保持旧 metadata 格式"
        store.close()

    def test_list_sessions_returns_project_id(self) -> None:
        from bandi.api.app import _get_store

        store = _get_store()
        store.create_session("s-with-project", title="项目会话", project_id="p-alpha")
        store.create_session("s-plain", title="普通会话")

        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        by_id = {s["id"]: s for s in resp.json()}
        assert by_id["s-with-project"]["project_id"] == "p-alpha"
        assert by_id["s-plain"]["project_id"] == ""


class TestSessionRename:
    def test_patch_rename_visible(self) -> None:
        from bandi.api.app import _get_store

        _get_store().create_session("s-rename", title="旧标题")
        resp = client.patch("/api/sessions/s-rename", json={"title": "新标题"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "新标题"

        detail = client.get("/api/sessions/s-rename").json()
        assert detail["session"]["title"] == "新标题"

    def test_breaker_invalid_payloads(self) -> None:
        from bandi.api.app import _get_store

        _get_store().create_session("s-rename", title="旧标题")
        resp = client.patch("/api/sessions/s-rename", json={"title": "   "})
        assert resp.status_code == 400, "空标题必须 400"
        resp = client.patch("/api/sessions/ghost-session", json={"title": "x"})
        assert resp.status_code == 404, "不存在会话必须 404"


class TestSessionMetaDod12:
    def test_full_chain_create_rename_list(self) -> None:
        """DoD-12：建（带项目）→ 改名 → 列表同回 project_id 与 title。"""
        from bandi.api.app import _get_store

        _get_store().create_session(
            "s-chain", title="初始", project_id="p-beta", project_root="D:/b"
        )
        resp = client.patch("/api/sessions/s-chain", json={"title": "改后"})
        assert resp.status_code == 200
        listed = {s["id"]: s for s in client.get("/api/sessions").json()}
        assert listed["s-chain"]["title"] == "改后"
        assert listed["s-chain"]["project_id"] == "p-beta"
