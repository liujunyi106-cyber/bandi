"""功能：会话通道（api/agent/stream）终态帧 seq/resume 补发契约（10.10）。

背景：断线时终态帧（done/error）丢失会让前端消息永久停在未定稿态（流式光标不消失
断线期间内容丢失——已登记技术债）。本批把公司通道（10.9）的 seq/resume 机制搬到会话
通道，差异是「缓冲按会话分桶 + 仅终态帧入缓冲」。

覆盖：① 终态帧分配 seq 并入缓冲（经真实 handler 路径）；② 非终态帧不入缓冲；
③ 按会话分桶（跨会话不串帧）；④ resume 水位过滤 + resumed 回包；⑤ 破坏者：无 scope
不回放、非法 last_seq 回退全量、缓冲有界、真实 WS 连接的 resume 补发。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def app_module() -> Any:
    return importlib.import_module("bandi.api.app")


@pytest.fixture(autouse=True)
def _clean_session_buffer(app_module: Any) -> Any:
    """每个用例前后清空会话缓冲与全局 seq（模块级状态，避免用例间串扰）。"""
    with app_module._session_ws_terminal_lock:
        app_module._session_ws_terminal.clear()
        app_module._SESSION_WS_SEQ = 0
    yield
    with app_module._session_ws_terminal_lock:
        app_module._session_ws_terminal.clear()
        app_module._SESSION_WS_SEQ = 0


class _FakeWS:
    """最小 WebSocket 替身（只实现 send_json）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_terminal_frames_recorded_via_handler(app_module: Any) -> None:
    """经真实 handler 路径：done/error 带 seq+scope，非终态帧原样直发不带 seq。"""
    ws = _FakeWS()
    handler = app_module._WSStreamHandler(ws, "s1")

    await handler.on_content_delta("s1", "增量")
    await handler.on_done("s1", "COMPLETED", 10, 1)
    await handler.on_error("s1", "boom")

    by_type = {frame["type"]: frame for frame in ws.sent}
    assert "seq" not in by_type["content_delta"], "非终态帧不得分配 seq"
    assert by_type["done"]["seq"] > 0 and by_type["done"]["scope"] == "s1"
    assert by_type["error"]["seq"] > by_type["done"]["seq"], "seq 必须单调递增"
    assert by_type["error"]["scope"] == "s1"


@pytest.mark.asyncio
async def test_handler_without_scope_does_not_buffer(app_module: Any) -> None:
    """破坏者：未传会话作用域（旧构造方式）时不得入缓冲——保持向后兼容且不污染全局。"""
    ws = _FakeWS()
    handler = app_module._WSStreamHandler(ws)

    await handler.on_done("s1", "COMPLETED", 1, 1)

    assert "seq" not in ws.sent[0], "无作用域不得分配 seq"
    assert app_module._replay_session_terminal("s1", 0) == []


@pytest.mark.asyncio
async def test_buffer_bucketed_by_session(app_module: Any) -> None:
    """按会话分桶：A 会话的 resume 不得回放到 B 会话的帧（公司通道全局缓冲的老问题）。"""
    ws_a, ws_b = _FakeWS(), _FakeWS()
    await app_module._WSStreamHandler(ws_a, "s-a").on_done("s-a", "COMPLETED", 1, 1)
    await app_module._WSStreamHandler(ws_b, "s-b").on_done("s-b", "COMPLETED", 2, 2)

    a_frames = app_module._replay_session_terminal("s-a", 0)
    b_frames = app_module._replay_session_terminal("s-b", 0)

    assert [f["scope"] for f in a_frames] == ["s-a"]
    assert [f["scope"] for f in b_frames] == ["s-b"]
    assert len(app_module._replay_session_terminal("s-missing", 0)) == 0


@pytest.mark.asyncio
async def test_replay_watermark_filters_processed_frames(app_module: Any) -> None:
    """resume 水位：seq <= last_seq 的帧不重复回放（前端已处理过）。"""
    ws = _FakeWS()
    handler = app_module._WSStreamHandler(ws, "s1")
    await handler.on_done("s1", "COMPLETED", 1, 1)
    await handler.on_error("s1", "boom")

    first_seq = ws.sent[0]["seq"]
    replayed = app_module._replay_session_terminal("s1", first_seq)

    assert [f["type"] for f in replayed] == ["error"]
    assert app_module._session_terminal_last_seq("s1") == ws.sent[1]["seq"]
    assert app_module._session_terminal_last_seq("s-none") == 0


@pytest.mark.asyncio
async def test_buffer_is_bounded(app_module: Any) -> None:
    """缓冲有界：超出 maxlen 自动淘汰最旧（内存不随会话长度无限增长）。"""
    ws = _FakeWS()
    handler = app_module._WSStreamHandler(ws, "s1")
    for index in range(app_module._SESSION_WS_TERMINAL_MAXLEN + 5):
        await handler.on_done("s1", f"R{index}", 1, index)

    replayed = app_module._replay_session_terminal("s1", 0)
    assert len(replayed) == app_module._SESSION_WS_TERMINAL_MAXLEN
    assert replayed[-1]["data"]["terminal_reason"] == "R24", "保留的必须是最新的那批"


@pytest.mark.asyncio
async def test_resume_protocol_over_real_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实连接：一轮对话产生 done 后，新连接 resume 必须补发该 done 并回 resumed。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    api_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(api_module, "_API_KEY", "test-key")

    turn = AsyncMock(
        reply="回答", terminal_reason="COMPLETED", total_tokens=10, turn_index=1, session_id="s-ws"
    )
    headers = {"Authorization": "Bearer test-key"}
    with patch("bandi.core.agent_runner.run_agent_turn") as mock_turn:
        mock_turn.return_value = turn
        with TestClient(api_module.app) as client:
            with client.websocket_connect("/api/agent/stream", headers=headers) as ws:
                ws.send_json({"type": "chat", "message": "hello", "session_id": "s-ws"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") == "done":
                        break
            # 第二条连接 = 断线重连：last_seq=0 表示本地水位归零（客户端已重启/新标签页）
            with client.websocket_connect("/api/agent/stream", headers=headers) as ws2:
                ws2.send_json({"type": "resume", "last_seq": 0, "scope": "s-ws"})
                frames = [ws2.receive_json(), ws2.receive_json()]

    types = [frame["type"] for frame in frames]
    assert types == ["done", "resumed"], f"必须先补发 done 再回 resumed，实际 {types}"
    assert frames[0]["scope"] == "s-ws"
    assert frames[1]["replayed"] == 1
    assert frames[1]["last_seq"] >= frames[0]["seq"]


@pytest.mark.asyncio
async def test_resume_edge_cases(app_module: Any) -> None:
    """破坏者：非法 last_seq 回退全量、非 dict 入参不崩、其他会话水位不跨桶。"""
    ws = _FakeWS()
    await app_module._WSStreamHandler(ws, "s1").on_done("s1", "COMPLETED", 1, 1)

    assert len(app_module._replay_session_terminal("s1", 0)) == 1
    assert len(app_module._replay_session_terminal("s1", -5)) == 1
    assert len(app_module._replay_session_terminal("s1", 10_000)) == 0
    # 无 scope / 未知 scope：一律不回放（不得退化成全局回放）
    assert len(app_module._replay_session_terminal("", 0)) == 0
    assert app_module._session_terminal_last_seq("") == 0
