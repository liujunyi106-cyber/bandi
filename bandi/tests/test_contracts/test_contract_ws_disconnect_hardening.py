"""功能：会话 WS 连接加固契约。

背景：
- 后端在**已发出 close 的连接**上继续推流式帧，Starlette 抛的是
 `RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'.")`，
 而 `_send` 只捕 `WebSocketDisconnect` ⇒ 漏判 ⇒ 整轮**每帧都重试失败**（实测 256 次刷 ERROR），
 前端一个帧都收不到。
- 回合此前是**内联 await** ⇒ 接收循环整轮失聪：前端 25s 心跳的 ping 无人应答，
 超 75s 无入站帧即主动断开重连（实测抖动来源），重连后旧连接上的推送全部落空。

钉行为不钉实现：
1. 任何发送失败（含「close 后 send」的 RuntimeError）都只记一次断连，后续帧**静默丢弃不再重试**
2. 真实取消（`asyncio.CancelledError`）不得被吞
3. 终态帧**在发送失败时仍入 resume 缓冲**（断连后重连仍能补发 done）
4. 回合执行期间接收循环**仍能服务 ping**（非阻塞）
5. 回合异常**回一帧 error**（后台任务不静默吞掉失败）"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

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


class _RaisingWS:
    """发送必失败的 WebSocket 替身——记录被调用次数（证明「失败后不再重试」）。"""

    def __init__(self, exc: BaseException) -> None:
        self.calls = 0
        self._exc = exc

    async def send_json(self, data: dict[str, Any]) -> None:
        self.calls += 1
        raise self._exc


_CLOSE_ERROR = RuntimeError(
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."
)


class TestSendStopsAfterFailure:
    @pytest.mark.asyncio
    async def test_close_error_marks_disconnected_without_raising(self, app_module: Any) -> None:
        """破坏者核心：「close 后 send」的 RuntimeError 不得外抛，且后续帧不再重试。"""
        ws = _RaisingWS(_CLOSE_ERROR)
        handler = app_module._WSStreamHandler(ws, "s1")

        for _ in range(5):
            await handler.on_content_delta("s1", "增量")  # 不得抛

        assert ws.calls == 1, f"失败后仍在重试发送：calls={ws.calls}"
        assert handler._disconnected is True

    @pytest.mark.asyncio
    async def test_websocket_disconnect_same_behavior(self, app_module: Any) -> None:
        """同族：真正的 WebSocketDisconnect 也走同一条「记一次即停」路径。"""
        from starlette.websockets import WebSocketDisconnect

        ws = _RaisingWS(WebSocketDisconnect(code=1006))
        handler = app_module._WSStreamHandler(ws, "s1")

        await handler.on_content_delta("s1", "增量")
        await handler.on_content_delta("s1", "增量")

        assert ws.calls == 1
        assert handler._disconnected is True

    @pytest.mark.asyncio
    async def test_real_cancellation_not_swallowed(self, app_module: Any) -> None:
        """契约：CancelledError 是 BaseException，不得被发送失败的捕获吞掉。"""
        ws = _RaisingWS(asyncio.CancelledError())
        handler = app_module._WSStreamHandler(ws, "s1")

        with pytest.raises(asyncio.CancelledError):
            await handler.on_content_delta("s1", "增量")


class TestTerminalFrameSurvivesSendFailure:
    @pytest.mark.asyncio
    async def test_done_buffered_for_resume_even_when_send_fails(self, app_module: Any) -> None:
        """断连后重连仍能拿到 done：终态帧在发送尝试之前就入了会话缓冲。"""
        ws = _RaisingWS(_CLOSE_ERROR)
        handler = app_module._WSStreamHandler(ws, "s9")

        await handler.on_done("s9", "COMPLETED", 10, 1)

        replayed = app_module._replay_session_terminal("s9", 0)
        assert [f["type"] for f in replayed] == ["done"], "断连场景下 done 未入 resume 缓冲"


def _isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """功能：隔离 home/db/store 的 TestClient。"""
    from fastapi.testclient import TestClient

    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    app_mod = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    SessionStore(str(db_path)).close()
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    monkeypatch.setattr(BandiConfig, "load", classmethod(lambda cls: cfg))
    # WS 路由：未配 key 时按 client host 白名单（TestClient 的 host 是 testclient）
    # ⇒ 用静态 key + 请求头走鉴权分支（与既有 WS 契约测试同法）
    monkeypatch.setattr(app_mod, "_API_KEY", "test")
    monkeypatch.setattr(app_mod, "_db_path", db_path)
    monkeypatch.setattr(app_mod, "_store", None)
    return TestClient(app_mod.app)


class _StubResult:
    session_id = "s-ws"
    terminal_reason = "COMPLETED"
    total_tokens = 1
    turn_index = 1


class TestReceiveLoopStaysResponsive:
    def test_ping_served_before_turn_finishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """核心契约：回合在跑时 ping 必须先于回合的 done 帧被服务。

        判定方式 = **帧序**（不依赖计时阈值）：回合桩固定睡 0.8s 后返回，
        ping 紧跟 chat 发出。接收循环若被回合阻塞（内联 await），
        帧序必然是 [done, pong]；非阻塞时 pong 先行 ⇒ [pong, done]。
        """
        calls: list[str] = []

        async def _slow_turn(**kwargs: Any) -> Any:
            calls.append("turn")
            await asyncio.sleep(0.8)
            return _StubResult()

        monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", _slow_turn)
        client = _isolated_client(tmp_path, monkeypatch)
        with client.websocket_connect(
            "/api/agent/stream", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"type": "chat", "message": "hi", "session_id": "s-ws"})
            ws.send_json({"type": "ping"})
            first = ws.receive_json()
            second = ws.receive_json()
            assert calls == ["turn"], "回合桩未生效"
            assert first["type"] == "pong", (
                f"回合运行中未先服务 ping（帧序 {first['type']} → {second['type']}）"
            )
            assert second["type"] == "done"

    def test_turn_failure_sends_error_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """后台任务边界：回合抛错必须回一帧 error（不得静默吞掉）。"""

        async def _boom(**kwargs: Any) -> Any:
            raise RuntimeError("kv-cache exploded")

        monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", _boom)
        client = _isolated_client(tmp_path, monkeypatch)
        with client.websocket_connect(
            "/api/agent/stream", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"type": "chat", "message": "hi", "session_id": "s-ws"})
            frame = ws.receive_json()
            assert frame["type"] == "error"
            assert "kv-cache exploded" in frame["data"]["message"]
