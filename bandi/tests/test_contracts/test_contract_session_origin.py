# 功能：契约——秘书会话来源标记（origin=secretary）。
# 钉行为：create_session(origin) 落 metadata（缺省不写，旧格式零变化）/
# （非 secretary 一律空，破坏者视角）。
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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


class TestSessionOrigin:
    def test_store_records_origin(self, tmp_path: Path) -> None:
        store = SessionStore(str(tmp_path / ".bandi" / "data" / "bandi.db"))
        sid = store.create_session(origin="secretary", project_id="p-alpha")
        meta = json.loads(store.get_session(sid)["metadata"])
        assert meta["origin"] == "secretary"
        assert meta["project_id"] == "p-alpha"

        plain = store.create_session()
        plain_meta = json.loads(store.get_session(plain)["metadata"])
        assert "origin" not in plain_meta, "缺省不写 origin（旧格式零变化）"
        store.close()

    def test_list_sessions_returns_origin(self) -> None:
        from bandi.api.app import _get_store

        store = _get_store()
        store.create_session("s-sec", title="项目秘书", project_id="p-alpha", origin="secretary")
        store.create_session("s-plain", title="普通")
        by_id = {s["id"]: s for s in client.get("/api/sessions").json()}
        assert by_id["s-sec"]["origin"] == "secretary"
        assert by_id["s-plain"]["origin"] == ""

    @pytest.mark.asyncio
    async def test_run_agent_turn_passes_origin(self, tmp_path: Path) -> None:
        """生产链：run_agent_turn(session_origin=) → 新会话 metadata 落库。"""
        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.config import BandiConfig
        from bandi.llm.mock import MockProvider

        store = SessionStore(str(tmp_path / ".bandi" / "data" / "bandi.db"))
        provider = MockProvider()
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="你好",
                session_id="s-origin-e2e",
                session_store=store,
                session_origin="secretary",
                bandi_config=BandiConfig(),
            )
        meta = json.loads(store.get_session("s-origin-e2e")["metadata"])
        assert meta["origin"] == "secretary"
        store.close()

    @pytest.mark.asyncio
    async def test_ws_origin_whitelist_breaker(self) -> None:
        """破坏者：WS payload 传非白名单 origin → 归一为空（不落库）。"""
        from bandi.api.app import _handle_ws_chat

        captured: dict[str, Any] = {}

        async def fake_turn(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                reply="ok",
                terminal_reason="COMPLETED",
                total_tokens=0,
                turn_index=1,
                session_id="s-ws",
            )

        class _FakeWS:
            async def send_json(self, data: dict[str, Any]) -> None: ...

        with patch("bandi.core.agent_runner.run_agent_turn", side_effect=fake_turn):
            await _handle_ws_chat(
                _FakeWS(),
                {
                    "type": "chat",
                    "message": "hi",
                    "session_id": "s-ws",
                    "session_origin": "secretary",
                },
            )
        assert captured["session_origin"] == "secretary"

        captured.clear()
        with patch("bandi.core.agent_runner.run_agent_turn", side_effect=fake_turn):
            await _handle_ws_chat(
                _FakeWS(),
                {
                    "type": "chat",
                    "message": "hi",
                    "session_id": "s-ws-2",
                    "session_origin": "admin",
                },
            )
        assert captured["session_origin"] == "", "非白名单 origin 必须归一为空"
