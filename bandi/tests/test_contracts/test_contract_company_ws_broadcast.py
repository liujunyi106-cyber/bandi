# 功能：契约——Chat-First 面板 WS 广播链：真实双连接收发 e2e + per-connection send
# 串行化（同一连接发送任务永不并发进入）+ 单连接失败不阻断其余广播（破坏者）。
# 钉行为不钉实现：真人双开连接发消息，两个连接都必须现场收到广播帧；
# 并发推送同一连接不得产生交叠 send（Starlette WS 单连接不支持并发 send）。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 装配隔离：Path.home→tmp + 重置全局 dispatcher（每次重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _receive_until(ws: Any, wanted: str, tries: int = 6) -> dict[str, Any]:
    """功能：循环收帧直到命中目标类型（广播 task 与 ok 帧到达顺序不定）。"""
    for _ in range(tries):
        frame = ws.receive_json()
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"未收到 {wanted} 帧")


# 真实双连接：端点消息触发广播 → 两个在线连接均现场收到 message 帧
def test_broadcast_reaches_all_open_connections() -> None:
    with client.websocket_connect("/ws/company", headers={"authorization": "Bearer test"}) as ws_a:
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws_b:
            ws_a.send_json({"content": "大家好", "sender_role": "user"})

            frame_a = _receive_until(ws_a, "message")
            frame_b = _receive_until(ws_b, "message")
            assert frame_a["data"]["body"] == "大家好"
            assert frame_b["data"]["body"] == "大家好"
            assert isinstance(frame_b.get("seq"), int)


# per-connection 串行化：并发推送同一连接时 send 永不交叠（max_active == 1）
def test_company_ws_send_is_serialized_per_connection() -> None:
    import importlib

    app_module = importlib.import_module("bandi.api.app")

    class FakeWS:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.frames: list[dict[str, Any]] = []

        async def send_json(self, frame: dict[str, Any]) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.frames.append(frame)
            self.active -= 1

    async def _run() -> FakeWS:
        fake = FakeWS()
        await asyncio.gather(
            *(app_module._company_ws_send(fake, {"i": index}) for index in range(8))
        )
        return fake

    fake = asyncio.run(_run())
    assert fake.max_active == 1, "同一连接的发送必须串行（不得并发 enter send_json）"
    assert len(fake.frames) == 8


# 破坏者：单连接 send 失败不得阻断其余连接广播
def test_broadcast_isolates_failing_connection() -> None:
    import importlib

    app_module = importlib.import_module("bandi.api.app")

    class FakeWS:
        def __init__(self, healthy: bool) -> None:
            self.healthy = healthy
            self.frames: list[dict[str, Any]] = []

        async def send_json(self, frame: dict[str, Any]) -> None:
            await asyncio.sleep(0.005)
            if not self.healthy:
                raise RuntimeError("broken pipe")
            self.frames.append(frame)

    async def _run() -> FakeWS:
        good, bad = FakeWS(healthy=True), FakeWS(healthy=False)
        app_module._company_ws_clients.add(good)
        app_module._company_ws_clients.add(bad)
        try:
            app_module._broadcast_company_message({"probe": "isolation"})
            await asyncio.sleep(0.1)
        finally:
            app_module._company_ws_clients.discard(good)
            app_module._company_ws_clients.discard(bad)
        return good

    good = asyncio.run(_run())
    assert good.frames, "健康连接必须收到广播（失败连接被隔离）"
    assert good.frames[0]["data"]["probe"] == "isolation"


def test_subscribe_frame_acks_scopes_and_cleans_up() -> None:
    """过程帧按项目定向的入口：客户端 subscribe 后服务端按连接记订阅；
    断连必须清理（`id(ws)` 可能被复用，残留会让新连接继承旧订阅）；
    `project_ids` 非法（非列表）→ 视为取消订阅回到 fail-open（不把客户端静默关进「什么都不收」）。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")
    before = dict(app_module._company_ws_scopes)

    with client.websocket_connect("/ws/company", headers={"authorization": "Bearer test"}) as ws:
        ws.send_json({"type": "subscribe", "project_ids": ["pA"]})
        # -首帧 hello（能力握手）先于 ack —— 按类型取帧
        ack = _receive_until(ws, "subscribed")
        assert ack["type"] == "subscribed"
        assert ack["scoped"] is True
        assert ack["project_ids"] == ["pA"]
        assert any(
            value == {"pA"} and key not in before
            for key, value in app_module._company_ws_scopes.items()
        ), "订阅必须记录在本连接上（否则定向不生效）"

    assert app_module._company_ws_scopes == before, "断连必须清理该连接的订阅记录"

    with client.websocket_connect("/ws/company", headers={"authorization": "Bearer test"}) as ws2:
        ws2.send_json({"type": "subscribe", "project_ids": "pA"})
        ack2 = _receive_until(ws2, "subscribed")
        assert ack2["type"] == "subscribed"
        assert ack2["scoped"] is False, (
            "非法 project_ids 必须取消订阅（fail-open），不是空集 fail-closed"
        )
        assert app_module._company_ws_scopes == before
