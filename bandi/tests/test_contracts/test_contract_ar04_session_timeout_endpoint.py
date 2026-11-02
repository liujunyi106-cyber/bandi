# 功能：契约——端点面 session_timeout_sec 透传（HTTP chat / SSE stream / WS）
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import importlib

    api_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(api_module, "_API_KEY", "test-key")
    return tmp_path


def _fake_turn_result() -> Any:
    return AsyncMock(
        reply="ok",
        terminal_reason="COMPLETED",
        total_tokens=1,
        turn_index=1,
        session_id="s-t",
    )


def test_chat_endpoint_forwards_session_timeout(home: Any) -> None:
    """契约：/api/agent/chat 必须把 body.session_timeout_sec
    透传 run_agent_turn（此前仅内核透传、端点面缺失）。"""
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        client = TestClient(app)
        resp = client.post(
            "/api/agent/chat",
            json={"message": "hi", "session_timeout_sec": 7},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200, f"端点应可达，实际 {resp.status_code}"
        assert mock_turn.call_args.kwargs.get("session_timeout_sec") == 7


def test_chat_endpoint_timeout_defaults_enabled(home: Any) -> None:
    """契约：未传 session_timeout_sec 时透传正数默认（看门狗启用）。

    缺省关闭曾导致回合卡在等待态时永不产出终帧、会话锁不释放；显式传 0 仍可关闭。
    """
    from fastapi.testclient import TestClient

    from bandi.api.app import _DEFAULT_SESSION_TIMEOUT_SEC, app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        client = TestClient(app)
        resp = client.post(
            "/api/agent/chat",
            json={"message": "hi"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        assert mock_turn.call_args.kwargs.get("session_timeout_sec") == _DEFAULT_SESSION_TIMEOUT_SEC
        assert _DEFAULT_SESSION_TIMEOUT_SEC > 0, "缺省须启用看门狗"


def test_stream_endpoint_forwards_session_timeout(home: Any) -> None:
    """契约：/api/agent/stream 同样透传 session_timeout_sec。"""
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        client = TestClient(app)
        resp = client.post(
            "/api/agent/stream",
            json={"message": "hi", "session_timeout_sec": 9},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        assert "event:" in resp.text
        assert mock_turn.call_args.kwargs.get("session_timeout_sec") == 9


def test_ws_endpoint_forwards_session_timeout(home: Any) -> None:
    """契约：WS /api/agent/stream 同样透传 session_timeout_sec。"""
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/agent/stream",
                headers={"Authorization": "Bearer test-key"},
            ) as ws:
                ws.send_json(
                    {
                        "type": "chat",
                        "message": "hello",
                        "session_id": "s-t",
                        "session_timeout_sec": 5,
                    }
                )
                import time

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        frame = ws.receive_json()
                    except Exception:  # noqa: BLE001 - 断开即停止
                        break
                    if frame.get("type") == "done":
                        break
        assert mock_turn.call_args.kwargs.get("session_timeout_sec") == 5
