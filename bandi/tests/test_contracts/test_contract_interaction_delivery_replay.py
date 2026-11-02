"""契约：交互帧投递 fail-closed（P1）+ WS 路径 abort 注册（P2）+ 挂起交互重放（P3）。

背景（2026-10-15 真机实证）：
- P1 交互帧 `ws.send_json` 在客户端已离开的半开连接上永久阻塞 → 整轮 executing 僵尸
  （事件日志零条目、1800s TTL 形同虚设）。
- P2 WS 聊天路径从不注册 AbortController → `POST /abort` 恒 404，停止按钮永远无效
  （SSE 路径有注册，WS 漏了；abort e2e 契约测试曾**手工预置** controller 掩盖该缺口）。
- P3 交互帧只在挂起瞬间推送一次，页面刷新/重连后 pending 卡永久丢失。

行为不变式：
  1. 推帧回调永不返回（阻塞）→ 按帧超时拒绝挂起，ask_permission 快速返回 False
  2. 推帧回调抛异常 → 同上 fail-closed；store 中该交互标记 rejected（晚到决策 409）
  3. 推帧正常 → 挂起照旧（决策可达）
  4. WS 聊天轮次运行期间 controller 已注册；session_abort 可终止；结束后注销
  5. pending 交互重放端点返回带 expires_in 的帧；resolve 后为空
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bandi.core.user_interaction import (
    HTTPUserInteraction,
    InteractionStore,
)


class _BlockingCallback:
    """功能：永不完成的推帧回调（模拟客户端半开连接上的阻塞 send）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, item: Any) -> None:
        self.calls += 1
        await asyncio.sleep(3600)


class _ExplodingCallback:
    """功能：推帧即抛异常的回调。"""

    async def __call__(self, item: Any) -> None:
        raise RuntimeError("connection closed")


@pytest.mark.asyncio
async def test_blocking_frame_send_fails_closed_fast() -> None:
    """破坏者：推帧永久阻塞 → 权限询问必须在帧超时内返回 False（轮次可推进）。"""
    store = InteractionStore()
    ui = HTTPUserInteraction(
        store, "s1", on_interaction=_BlockingCallback(), frame_send_timeout=0.05
    )
    loop = asyncio.get_running_loop()
    start = loop.time()
    granted = await ui.ask_permission(tool_name="bash", args={}, risk="high")
    elapsed = loop.time() - start
    assert granted is False
    assert elapsed < 2.0, f"推帧阻塞必须在帧超时内 fail-closed，实际 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_delivery_failure_rejects_interaction() -> None:
    """推帧失败 → 该交互被标记 rejected（晚到 POST /confirm 得 409 而非静默批准）。"""
    store = InteractionStore()
    ui = HTTPUserInteraction(
        store, "s2", on_interaction=_ExplodingCallback(), frame_send_timeout=0.05
    )
    granted = await ui.ask_permission(tool_name="bash", args={}, risk="high")
    assert granted is False
    pending = store.list_pending(session_id="s2")
    assert pending == [], "推送失败的挂起不得残留 pending"
    assert store._items and all(item.status == "rejected" for item in store._items.values()), (
        "失败交互必须显式拒绝"
    )


@pytest.mark.asyncio
async def test_normal_delivery_still_suspends() -> None:
    """推帧正常时行为不变：挂起等待决策，resolve 唤醒。"""
    store = InteractionStore()
    seen: dict = {}

    async def on_interaction(item: Any) -> None:
        seen["item"] = item

    ui = HTTPUserInteraction(store, "s3", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_permission(tool_name="bash", args={}, risk="high"))
    for _ in range(100):
        if "item" in seen:
            break
        await asyncio.sleep(0.01)
    store.resolve(seen["item"].interaction_id, True)
    assert await task is True


# ── P2：WS 路径 abort 注册 ──────────────────────────────────────────────


class _FakeWebSocket:
    """功能：最小 WS 桩——记录 send_json 调用，不真正联网。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_ws_chat_registers_and_unregisters_abort_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WS 聊天轮次：运行期 controller 已注册（session_abort 可终止），结束后注销。"""
    import sys

    import bandi.api.app  # noqa: F401 —— __init__ re-export 会把名字绑成 FastAPI 实例
    import bandi.core.agent_runner as agent_runner

    app_module = sys.modules["bandi.api.app"]  # 取真模块（见 memory 坑：import-as 拿到实例）
    from bandi.core.abort_controller import AbortController

    app_module._active_abort_controllers.clear()

    class _FakeResult:
        session_id = "sess-ws-abort"
        terminal_reason = "stop"
        total_tokens = 0
        turn_index = 1

    abort_seen: dict = {}

    async def fake_run_agent_turn(**kwargs: Any) -> Any:
        ctrl = kwargs.get("abort_controller")
        abort_seen["ctrl"] = ctrl
        assert isinstance(ctrl, AbortController)
        assert app_module._active_abort_controllers.get("sess-ws-abort") is ctrl
        # 模拟挂起中的轮次：直到被 abort 才返回
        while not ctrl.should_abort:
            await asyncio.sleep(0.01)
        return _FakeResult()

    monkeypatch.setattr(agent_runner, "run_agent_turn", fake_run_agent_turn)

    ws = _FakeWebSocket()
    payload = {"message": "hi", "session_id": "sess-ws-abort"}
    task = asyncio.create_task(app_module._handle_ws_chat(ws, payload))
    for _ in range(200):
        if "ctrl" in abort_seen:
            break
        await asyncio.sleep(0.01)
    assert "ctrl" in abort_seen, "轮次启动后 abort controller 必须已注册"

    result = await app_module.session_abort("sess-ws-abort")
    assert result["aborted"] is True

    await asyncio.wait_for(task, timeout=5.0)
    assert app_module._active_abort_controllers.get("sess-ws-abort") is None, (
        "轮结束后 controller 必须注销（否则新轮 abort 语义被旧条目遮蔽）"
    )
    app_module._active_abort_controllers.clear()


# ── P3：挂起交互重放端点 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_interactions_replay_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话加载补拉：pending 交互返回带 expires_in 的帧；决策后重放为空。"""
    import sys

    import bandi.api.app  # noqa: F401

    app_module = sys.modules["bandi.api.app"]

    # 隔离全局单例（测试用独立 store）
    isolated_store = InteractionStore()
    monkeypatch.setattr(app_module, "_interaction_store", isolated_store)

    async def _noop(item: Any) -> None:
        return None

    ui = HTTPUserInteraction(isolated_store, "sess-replay", on_interaction=_noop)
    task = asyncio.create_task(ui.ask_confirmation("继续吗？"))
    for _ in range(100):
        if isolated_store.list_pending(session_id="sess-replay"):
            break
        await asyncio.sleep(0.01)

    payload = await app_module.session_pending_interactions("sess-replay")
    frames = payload["interactions"]
    assert len(frames) == 1
    frame = frames[0]
    assert frame["kind"] == "confirm"
    assert frame["payload"]["prompt"] == "继续吗？"
    assert isinstance(frame.get("expires_in"), int) and frame["expires_in"] > 0

    isolated_store.resolve(frame["interaction_id"], True)
    await task
    payload_after = await app_module.session_pending_interactions("sess-replay")
    assert payload_after["interactions"] == [], "决策后重放必须为空"
    monkeypatch.setattr(app_module, "_interaction_store", None)
