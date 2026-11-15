# 功能：permission 挂起 TTL 契约——长轮次中途的授权等待窗口（30 分钟）独立于
# wait_timeout；confirm/input 保持短超时语义；过期交互上的晚到决策必须被拒（不静默）。
from __future__ import annotations

import asyncio

import pytest

from bandi.core.user_interaction import (
    PERMISSION_INTERACTION_TTL,
    HTTPUserInteraction,
    InteractionStore,
)


async def _wait_callback(seen: dict, limit: float = 2.0) -> None:
    """等 on_interaction 回调送达（挂起方进入 register 需要数个调度拍）。"""
    for _ in range(200):
        if "item" in seen:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("on_interaction 回调未送达")


@pytest.mark.asyncio
async def test_ask_permission_registers_long_ttl() -> None:
    """permission 挂起窗口 = 30 分钟：用户在长轮次中途离开，回来仍可决策。"""
    store = InteractionStore()
    seen: dict = {}

    async def on_interaction(item: object) -> None:
        seen["item"] = item

    ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_permission(tool_name="ontology_assert", args={}, risk="high"))
    await _wait_callback(seen)
    item = seen["item"]
    assert item.ttl_seconds == PERMISSION_INTERACTION_TTL
    assert item.remaining_seconds() > PERMISSION_INTERACTION_TTL - 10, "出生即满窗（≈1800s）"

    store.resolve(item.interaction_id, True)
    assert await task is True
    assert store.get(item.interaction_id).status == "resolved"


@pytest.mark.asyncio
async def test_ask_permission_ttl_independent_of_wait_timeout() -> None:
    """破坏者：wait_timeout 调短不得连带砍掉 permission 窗口（两语义解耦）。"""
    store = InteractionStore()
    seen: dict = {}

    async def on_interaction(item: object) -> None:
        seen["item"] = item

    ui = HTTPUserInteraction(store, "s2", on_interaction=on_interaction, wait_timeout=1.0)
    task = asyncio.create_task(
        ui.ask_permission_with_feedback(tool_name="fs_write", args={}, risk="medium")
    )
    await _wait_callback(seen)
    assert seen["item"].ttl_seconds == PERMISSION_INTERACTION_TTL

    store.resolve(seen["item"].interaction_id, {"granted": True, "feedback": ""})
    granted, feedback = await task
    assert granted is True
    assert feedback == ""


@pytest.mark.asyncio
async def test_permission_ttl_constructor_override() -> None:
    """permission_ttl 构造参数可注入短窗口（fail-closed 测试与受限部署用）。"""
    store = InteractionStore()
    seen: dict = {}

    async def on_interaction(item: object) -> None:
        seen["item"] = item

    ui = HTTPUserInteraction(store, "s2b", on_interaction=on_interaction, permission_ttl=0.05)
    decision = await ui.ask_permission(tool_name="bash", args={}, risk="high")
    assert decision is False, "短窗口超时必须按拒绝解释（fail-closed）"
    assert store.get(seen["item"].interaction_id).status == "expired"


@pytest.mark.asyncio
async def test_ask_confirmation_keeps_short_timeout() -> None:
    """ask_confirmation 语义不变：默认 30s（min(入参, wait_timeout)）。"""
    store = InteractionStore()
    seen: dict = {}

    async def on_interaction(item: object) -> None:
        seen["item"] = item

    ui = HTTPUserInteraction(store, "s3", on_interaction=on_interaction)
    task = asyncio.create_task(ui.ask_confirmation("继续执行？"))
    await _wait_callback(seen)
    assert seen["item"].ttl_seconds == 30.0

    store.resolve(seen["item"].interaction_id, True)
    assert await task is True


@pytest.mark.asyncio
async def test_resolve_on_expired_interaction_refuses() -> None:
    """破坏者：get_decision 已按超时唤醒后，晚到 resolve 不得把交互标成 resolved
    （否则「超时拒绝已生效 + 卡片显示已超时」与「决策又算批准」互相矛盾）。"""
    store = InteractionStore()
    pending = store.register(session_id="s4", kind="permission", payload={})
    assert await store.get_decision(pending.interaction_id, timeout=0.05) is None
    assert store.resolve(pending.interaction_id, True) is False
    assert store.get(pending.interaction_id).status == "expired"
