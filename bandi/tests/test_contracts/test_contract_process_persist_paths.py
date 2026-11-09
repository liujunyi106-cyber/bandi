# 功能：契约——过程条目落库全路径：CLI 轮、异常轮（SSE/WS）都必须
# 落 process 条目；persist_partial 幂等且把 running 节点收敛为 error。
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _process_rows(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT session_id, metadata FROM session_entries WHERE entry_type = 'process'"
        ).fetchall()
    finally:
        conn.close()
    return rows


class _NullHandler:
    """功能：ProcessRecorder 内层的空实现（记录调用即可）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_tool_call_start(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("tool_start")

    async def on_tool_result(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("tool_result")

    async def on_thinking(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("thinking")


def _seed_leaf(store: Any, session_id: str) -> None:
    # 功能：造会话首条 user 消息（真实链路中 process 落库前必有 leaf）
    from bandi.core.message import Message, MessageRole

    store.append_message(session_id, Message(role=MessageRole.USER, content="hi"))


class TestPersistPartial:
    @pytest.mark.asyncio
    async def test_persist_partial_converges_and_is_idempotent(self, tmp_path: Path) -> None:
        from bandi.core.process_recorder import ProcessRecorder
        from bandi.memory.session.store import SessionStore

        db_path = tmp_path / "sessions.db"
        store = SessionStore(str(db_path))
        store.create_session("s1")
        _seed_leaf(store, "s1")
        recorder = ProcessRecorder(_NullHandler(), store, "s1")

        await recorder.on_tool_call_start("s1", "bash", {"command": "ls"}, "tc1")
        await recorder.persist_partial("s1", 3)

        rows = _process_rows(db_path)
        assert len(rows) == 1, "异常轮必须先落一条 process 条目"
        assert rows[0][0] == "s1"
        assert '"status": "error"' in rows[0][1], "被打断的 running 工具须收敛为 error"
        assert '"turn_index": 3' in rows[0][1]

        await recorder.persist_partial("s1", 3)
        assert len(_process_rows(db_path)) == 1, "二次调用必须幂等（节点已清空）"
        store.close()

    @pytest.mark.asyncio
    async def test_empty_nodes_no_entry(self, tmp_path: Path) -> None:
        from bandi.core.process_recorder import ProcessRecorder
        from bandi.memory.session.store import SessionStore

        db_path = tmp_path / "sessions.db"
        store = SessionStore(str(db_path))
        store.create_session("s1")
        _seed_leaf(store, "s1")
        recorder = ProcessRecorder(_NullHandler(), store, "s1")

        await recorder.persist_partial("s1", 1)
        assert _process_rows(db_path) == [], "无过程节点不得落空条目"
        store.close()


class TestCliProcessPersist:
    @pytest.mark.asyncio
    async def test_cli_execute_turn_persists_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 轮必须与 API 同口径落 process 条目（此前 CLI 链路缺失）。"""
        from io import StringIO
        from types import SimpleNamespace

        from rich.console import Console

        from bandi.cli.chat import ChatLoop
        from bandi.cli.chat_controller import QueuedChatInput
        from bandi.core.process_recorder import ProcessRecorder

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        captured: dict[str, Any] = {}

        async def fake_run_agent_turn(**kwargs: Any) -> Any:
            captured.update(kwargs)
            handler = kwargs["stream_handler"]
            await handler.on_tool_call_start("s1", "bash", {"command": "ls"}, "tc1")
            await handler.on_tool_result("s1", "bash", True, "ok", 5, "tc1")
            return SimpleNamespace(
                session_id="s1", terminal_reason="COMPLETED", total_tokens=1, turn_index=1
            )

        loop = ChatLoop(console=Console(file=StringIO(), force_terminal=False, width=120))
        loop._get_session_store().create_session("s1")
        _seed_leaf(loop._get_session_store(), "s1")
        with patch("bandi.core.agent_runner.run_agent_turn", new=fake_run_agent_turn):
            await loop._execute_turn(QueuedChatInput(text="hello"))

        assert isinstance(captured["stream_handler"], ProcessRecorder), (
            "CLI 必须经 ProcessRecorder 装配（轮末落 process）"
        )
        assert captured.get("session_store") is not None, "CLI 必须透传共享 SessionStore"
        rows = _process_rows(tmp_path / ".bandi" / "data" / "bandi.db")
        assert len(rows) == 1 and rows[0][0] == "s1", f"CLI 轮必须落 process 条目：{rows}"
        loop._get_session_store().close()

    @pytest.mark.asyncio
    async def test_cli_error_turn_persists_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 异常轮同样落过程（persist_partial），重抛不吞异常。"""
        from io import StringIO

        from rich.console import Console

        from bandi.cli.chat import ChatLoop
        from bandi.cli.chat_controller import QueuedChatInput

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def fake_run_agent_turn(**kwargs: Any) -> Any:
            handler = kwargs["stream_handler"]
            await handler.on_tool_call_start("s1", "bash", {}, "tc1")
            raise RuntimeError("boom")

        loop = ChatLoop(console=Console(file=StringIO(), force_terminal=False))
        loop._get_session_store().create_session("s1")
        _seed_leaf(loop._get_session_store(), "s1")
        with patch("bandi.core.agent_runner.run_agent_turn", new=fake_run_agent_turn):
            with pytest.raises(RuntimeError):
                await loop._execute_turn(QueuedChatInput(text="hello"))

        rows = _process_rows(tmp_path / ".bandi" / "data" / "bandi.db")
        assert len(rows) == 1, f"CLI 异常轮必须落 process 条目：{rows}"
        loop._get_session_store().close()


class TestApiErrorPathsPersist:
    @pytest.fixture()
    def api_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
        from bandi.core.config import BandiConfig
        from bandi.memory.session.store import SessionStore

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        db_path = tmp_path / ".bandi" / "data" / "bandi.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SessionStore(str(db_path))
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
            yield {"db_path": db_path}

    def test_sse_error_path_persists_process(self, api_env: dict[str, Path]) -> None:
        from fastapi.testclient import TestClient

        from bandi.api.app import app

        client = TestClient(app, raise_server_exceptions=False)
        client.headers["Authorization"] = "Bearer test"

        async def fake_run_agent_turn(**kwargs: Any) -> Any:
            handler = kwargs["stream_handler"]
            await handler.on_tool_call_start("x", "bash", {"command": "ls"}, "tc1")
            raise RuntimeError("provider exploded")

        with patch("bandi.core.agent_runner.run_agent_turn", new=fake_run_agent_turn):
            resp = client.post("/api/agent/stream", json={"message": "hello"})
        assert "event: error" in resp.text
        rows = _process_rows(api_env["db_path"])
        assert len(rows) == 1, f"SSE 异常轮必须落 process 条目：{rows}"

    def test_ws_error_path_persists_process(self, api_env: dict[str, Path]) -> None:
        from fastapi.testclient import TestClient

        from bandi.api.app import app

        client = TestClient(app, raise_server_exceptions=False)
        headers = {"Authorization": "Bearer test"}

        async def fake_run_agent_turn(**kwargs: Any) -> Any:
            handler = kwargs["stream_handler"]
            store = kwargs["session_store"]
            store.create_session("ws-default")
            _seed_leaf(store, "ws-default")
            await handler.on_tool_call_start("x", "bash", {"command": "ls"}, "tc1")
            raise RuntimeError("provider exploded")

        with patch("bandi.core.agent_runner.run_agent_turn", new=fake_run_agent_turn):
            try:
                with client.websocket_connect("/api/agent/stream", headers=headers) as ws:
                    ws.send_json({"type": "chat", "message": "hello"})
            except Exception:  # noqa: BLE001 - 异常轮后连接关闭属预期
                pass
        rows = _process_rows(api_env["db_path"])
        assert len(rows) == 1, f"WS 异常轮必须落 process 条目：{rows}"
