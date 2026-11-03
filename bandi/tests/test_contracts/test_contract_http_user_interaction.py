# 功能：HTTPUserInteraction 契约——真挂起闭环（register→推帧→await→resolve 唤醒同一 turn 续跑）
# 背景：API 场景（uvicorn）装配 HTTPUserInteraction 到 PermissionsPipeline/UserInputTool。
# （同一 turn 真挂起，事件循环释放）→ 用户 POST /confirm|/clarify → resolve/reject 唤醒恢复。
# 契约钉行为：ask_permission/ask_input 挂起→唤醒闭环、超时 fail-closed、reject、会话隔离。
from __future__ import annotations

import asyncio

import pytest

from bandi.core.user_interaction import (
    HTTPUserInteraction,
    InteractionStore,
    PendingInteraction,
)


def _collector() -> tuple[list[PendingInteraction], object]:
    seen: list[PendingInteraction] = []

    async def _on(item: PendingInteraction) -> None:
        seen.append(item)

    return seen, _on


@pytest.mark.asyncio
async def test_ask_permission_suspend_then_resolve_true() -> None:
    """真挂起闭环：ask_permission 挂起 → 用户批准 resolve(True) → 同一调用返回 True。"""
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)

    task = asyncio.create_task(ui.ask_permission("bash", {"command": "rm x"}, "high"))
    # 回调在事件循环下一轮才落地，慢机器上单次 sleep(0) 不够——轮询等到帧出现
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert task.done() is False, "用户决策前 ask_permission 必须保持挂起"
    assert len(seen) == 1, "交互帧已推送"
    assert seen[0].kind == "permission"
    assert seen[0].payload["tool_name"] == "bash"
    assert seen[0].session_id == "s1"

    store.resolve(seen[0].interaction_id, True)
    assert await task is True


@pytest.mark.asyncio
async def test_ask_permission_reject_returns_false() -> None:
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_permission("bash", {}, "medium"))
    await asyncio.sleep(0)
    store.reject(seen[0].interaction_id)
    assert await task is False


@pytest.mark.asyncio
async def test_ask_permission_timeout_fail_closed() -> None:
    """超时（permission_ttl 0.05 短窗口注入）→ 默认拒绝 False（fail-closed）。"""
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(
        store, session_id="s1", on_interaction=on_interaction, permission_ttl=0.05
    )
    result = await ui.ask_permission("bash", {}, "high")
    assert result is False
    assert seen[0].status == "expired"


@pytest.mark.asyncio
async def test_ask_input_returns_text_answer() -> None:
    """input 交互：/clarify 文本答复唤醒 → 返回字符串。"""
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_input("仓库地址?", secret=False))
    await asyncio.sleep(0)
    assert seen[0].kind == "input"
    store.resolve(seen[0].interaction_id, "https://example.com/repo.git")
    assert await task == "https://example.com/repo.git"


@pytest.mark.asyncio
async def test_ask_input_timeout_returns_empty() -> None:
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(
        store, session_id="s1", on_interaction=on_interaction, wait_timeout=0.05
    )
    result = await ui.ask_input("name?")
    assert result == ""


@pytest.mark.asyncio
async def test_ask_confirmation_resolve_roundtrip() -> None:
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_confirmation("确认删除？"))
    await asyncio.sleep(0)
    assert seen[0].kind == "confirm"
    store.resolve(seen[0].interaction_id, True)
    assert await task is True


@pytest.mark.asyncio
async def test_show_notification_pushes_frame_without_wait() -> None:
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)
    await ui.show_notification("进度提示", level="info")
    assert len(seen) == 1
    assert seen[0].payload.get("message") == "进度提示"


@pytest.mark.asyncio
async def test_default_input_ttl_matches_long_human_window() -> None:
    """契约：提问挂起窗口 ≥1800s（与 permission 同口径）——员工链路
    短窗让用户在办别的事时回来卡已死、晚到答复被弃（员工降级默认口径继续）。"""
    store = InteractionStore()
    seen, on_interaction = _collector()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_input("目标市场？"))
    await asyncio.sleep(0)
    item = seen[0]
    assert item.ttl_seconds >= 1800.0, "提问挂起窗口必须 ≥1800s（与 permission 同口径）"
    store.reject(item.interaction_id)
    assert await task == ""


@pytest.mark.asyncio
async def test_session_isolation() -> None:
    """不同 session 的 HTTPUserInteraction 互不干扰（决策只唤醒本会话挂起）。"""
    store = InteractionStore()
    seen1, on1 = _collector()
    seen2, on2 = _collector()
    ui1 = HTTPUserInteraction(store, session_id="sA", on_interaction=on1)
    ui2 = HTTPUserInteraction(store, session_id="sB", on_interaction=on2)
    t1 = asyncio.create_task(ui1.ask_permission("bash", {}, "high"))
    t2 = asyncio.create_task(ui2.ask_permission("write", {}, "high"))
    await asyncio.sleep(0)
    assert seen1[0].session_id == "sA"
    assert seen2[0].session_id == "sB"
    store.resolve(seen1[0].interaction_id, True)
    assert await t1 is True
    assert t2.done() is False, "sB 的挂起不受 sA 决策影响"
    store.reject(seen2[0].interaction_id)
    assert await t2 is False
