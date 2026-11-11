# 功能：Day132c InteractionStore 契约——pending 交互登记/真挂起恢复/超时/拒绝 生命周期
# 背景：confirm/clarify 交互协议的地基。LLM 工具执行路径遇交互需求时 register + await
# get_decision（同一 turn 内真挂起）；用户 POST /confirm|/clarify 经 resolve/reject 唤醒。
# 契约钉行为：register→await→resolve 恢复闭环、超时返回 None、状态机 pending→resolved/expired。
from __future__ import annotations

import asyncio

import pytest

from bandi.core.user_interaction import InteractionStore


@pytest.mark.asyncio
async def test_register_await_resolve_roundtrip() -> None:
    """register → await get_decision 真挂起 → resolve 唤醒恢复（同一 turn 续跑语义）。"""
    store = InteractionStore()
    pending = store.register(
        session_id="s1", kind="confirm", payload={"question": "删除 3 个文件？"}
    )
    assert pending.status == "pending"
    assert isinstance(pending.interaction_id, str) and pending.interaction_id

    task = asyncio.create_task(store.get_decision(pending.interaction_id))
    await asyncio.sleep(0)  # 让 get_decision 进入等待
    assert not task.done(), "resolve 前 get_decision 必须保持挂起（真挂起语义）"

    resolved = store.resolve(pending.interaction_id, True)
    assert resolved is True
    decision = await task
    assert decision is True
    assert store.get(pending.interaction_id).status == "resolved"


@pytest.mark.asyncio
async def test_register_await_input_value() -> None:
    """input 类交互 resolve 字符串答复。"""
    store = InteractionStore()
    pending = store.register(
        session_id="s2", kind="input", payload={"prompt": "API key?", "secret": True}
    )
    task = asyncio.create_task(store.get_decision(pending.interaction_id))
    await asyncio.sleep(0)
    store.resolve(pending.interaction_id, "sk-test-123")
    assert await task == "sk-test-123"


@pytest.mark.asyncio
async def test_timeout_returns_none_and_marks_expired() -> None:
    """超时默认拒绝：get_decision(timeout) 超时返回 None，pending 标记 expired。"""
    store = InteractionStore()
    pending = store.register(session_id="s3", kind="permission", payload={})
    decision = await store.get_decision(pending.interaction_id, timeout=0.05)
    assert decision is None, "超时必须返回 None（调用方按 kind 解释为拒绝/无答复）"
    assert store.get(pending.interaction_id).status == "expired"


@pytest.mark.asyncio
async def test_reject_returns_none() -> None:
    """reject（用户显式拒绝）→ get_decision 返回 None，状态 rejected。"""
    store = InteractionStore()
    pending = store.register(session_id="s4", kind="permission", payload={})
    task = asyncio.create_task(store.get_decision(pending.interaction_id))
    await asyncio.sleep(0)
    store.reject(pending.interaction_id)
    assert await task is None
    assert store.get(pending.interaction_id).status == "rejected"


@pytest.mark.asyncio
async def test_multi_pending_isolated() -> None:
    """同 session 多个 pending 互不干扰（并发决策 CAS 语义由各自 future 承载）。"""
    store = InteractionStore()
    p1 = store.register(session_id="s5", kind="confirm", payload={"q": 1})
    p2 = store.register(session_id="s5", kind="confirm", payload={"q": 2})
    t1 = asyncio.create_task(store.get_decision(p1.interaction_id))
    t2 = asyncio.create_task(store.get_decision(p2.interaction_id))
    await asyncio.sleep(0)
    store.resolve(p2.interaction_id, "second")
    store.resolve(p1.interaction_id, "first")
    assert await t2 == "second"
    assert await t1 == "first"
    assert len(store.list_pending(session_id="s5")) == 0, "全部 resolved 后无 pending 残留"


def test_list_pending_filters_by_session_and_kind() -> None:
    store = InteractionStore()
    store.register(session_id="s6", kind="input", payload={})
    store.register(session_id="s6", kind="confirm", payload={})
    store.register(session_id="s7", kind="input", payload={})
    assert len(store.list_pending(session_id="s6")) == 2
    assert len(store.list_pending(session_id="s6", kind="input")) == 1
    assert len(store.list_pending(session_id="s6", kind="confirm")) == 1
    assert len(store.list_pending(session_id="s7")) == 1
    assert len(store.list_pending()) == 3


def test_ttl_expires_stale_pending() -> None:
    """TTL（默认 ）：过期 pending 查询时自动标 expired（后端防堆积）。"""
    store = InteractionStore(ttl_seconds=0.01)
    pending = store.register(session_id="s8", kind="permission", payload={})
    assert pending.status == "pending"
    import time

    time.sleep(0.02)
    # 惰性过期：查询时回收
    stale = store.list_pending(session_id="s8")
    assert stale == [], "过期 pending 不应再出现在活跃列表"
    assert store.get(pending.interaction_id).status == "expired"


def test_resolve_unknown_id_returns_false() -> None:
    store = InteractionStore()
    assert store.resolve("no-such-id", True) is False
    assert store.reject("no-such-id") is False
