# 功能：WS/SSE 流式链路验证 — 真实推送 Agent 事件

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import importlib

    api_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(api_module, "_API_KEY", "test-key")
    return tmp_path


def _fake_turn_result() -> Any:
    from unittest.mock import AsyncMock

    return AsyncMock(
        reply="流式回答内容",
        terminal_reason="COMPLETED",
        total_tokens=10,
        turn_index=1,
        session_id="s-ws",
    )


@pytest.mark.asyncio
async def test_ws_chat_pushes_events(home: Any) -> None:
    """WS /api/agent/stream：chat 消息必须推送 content_delta + done 事件。"""
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/agent/stream",
                headers={"Authorization": "Bearer test-key"},
            ) as ws:
                ws.send_json({"type": "chat", "message": "hello", "session_id": "s-ws"})
                events = []
                import time

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        events.append(ws.receive_json())
                    except Exception:  # noqa: BLE001 - 断开即停止收集
                        break
                    if any(e.get("type") == "done" for e in events):
                        break
        assert events, "WS 必须推送事件"
        types = [e.get("type") for e in events]
        assert "done" in types, f"必须推送 done，实际 {types}"


@pytest.mark.asyncio
async def test_ws_stream_handler_forwards_events() -> None:
    """_WSStreamHandler 必须将 Agent 事件转发为 WS 消息（真实流式数据通道）。"""

    from bandi.api.app import _WSStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_content_delta("s1", "增量内容")
    await handler.on_tool_call_start("s1", "read", {"path": "x"})
    await handler.on_tool_result("s1", "read", True, "preview", 10)

    types = [m.get("type") for m in sent]
    assert "content_delta" in types, "content_delta 必须转发"
    assert "tool_call_start" in types, "tool_call_start 必须转发"
    assert "tool_result" in types, "tool_result 必须转发"
    assert sent[0]["data"]["text"] == "增量内容", "增量内容必须透传"


@pytest.mark.asyncio
async def test_sse_stream_returns_reply(home: Any) -> None:
    """SSE /api/agent/stream：真流式——事件逐帧转发，结束帧为 done。"""
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = _fake_turn_result()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/stream",
                json={"message": "hello"},
                headers={"Authorization": "Bearer test-key"},
            )
            body = resp.text
        assert resp.status_code == 200, f"SSE 应 200，实际 {resp.status_code}"
        assert "event: done" in body, "SSE 必须含 done 事件"
        assert '"COMPLETED"' in body or "COMPLETED" in body, "done 必须含 terminal_reason"


@pytest.mark.asyncio
async def test_sse_handler_emits_frames() -> None:
    from bandi.api.app import _SSEStreamHandler

    handler = _SSEStreamHandler()
    await handler.on_content_delta("s1", "增量")
    await handler.on_tool_call_start("s1", "read", {"path": "x"})
    await handler.on_tool_result("s1", "read", True, "preview", 10)
    await handler.put("done", {"terminal_reason": "COMPLETED"})

    frames = [await handler._queue.get() for _ in range(4)]
    assert any(f.startswith("event: content_delta") and "增量" in f for f in frames), frames
    assert any(f.startswith("event: tool_call_start") and '"read"' in f for f in frames), frames
    assert any(f.startswith("event: tool_result") for f in frames), frames
    assert frames[-1].startswith("event: done"), frames[-1]
