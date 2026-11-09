# 功能：人工审批决策事件契约——approval-decision 落库同时发可审计事件（8.2 收口）
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.tools.approval import (
    ApprovalStore,
    PendingApproval,
    apply_decision,
    params_digest,
)


def _store_with_pending(tmp_path: Path, session_id: str) -> ApprovalStore:
    store = ApprovalStore(tmp_path / "a.json")
    store.set_pending(
        session_id,
        PendingApproval(
            tool_call_id="c1",
            tool_name="bash",
            params_digest=params_digest({"command": "rm -rf build"}),
            reason="高危命令",
        ),
    )
    return store


async def _spy(bus: EventBus, *types: EventType) -> list[Event]:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    for event_type in types:
        await bus.subscribe(event_type, handler)
    return received


class TestManualDecisionEvents:
    @pytest.mark.asyncio
    async def test_manual_deny_emits_audit_event(self, tmp_path: Path) -> None:
        """人工 deny 必须发事件并带完整归属（谁/哪会话/哪工具/摘要）。"""
        store = _store_with_pending(tmp_path, "s1")
        bus = EventBus()
        events = await _spy(bus, EventType.APPROVAL_DENIED)
        store.bind_event_bus(bus, session_id="s1")

        reply = await apply_decision(store, "s1", "deny", "bash")

        assert "deny" in reply
        assert len(events) == 1, "deny 决策必须留下事件痕迹（此前无痕）"
        data = events[0].data
        assert data["tool_name"] == "bash"
        assert data["session_id"] == "s1"
        assert data["decided_by"] == "user"
        assert data["params_digest"]

    @pytest.mark.asyncio
    async def test_manual_allow_emits_event_and_keeps_grant(self, tmp_path: Path) -> None:
        """人工 allow 必须同时：发事件 + 授权真实可用（事件不替代授权语义）。"""
        store = _store_with_pending(tmp_path, "s1")
        bus = EventBus()
        events = await _spy(bus, EventType.APPROVAL_GRANTED)
        store.bind_event_bus(bus, session_id="s1")

        reply = await apply_decision(store, "s1", "allow_once", "bash")

        assert "approval_decision" in reply
        assert len(events) == 1
        assert events[0].data["decided_by"] == "user"
        digest = params_digest({"command": "rm -rf build"})
        assert store.is_granted("s1", "bash", params_digest=digest), "一次性授权必须落库"
        assert store.is_granted("s1", "bash", params_digest=digest), "one-shot 消费即焚"

    @pytest.mark.asyncio
    async def test_no_bound_bus_is_safe(self, tmp_path: Path) -> None:
        """未绑定总线（独立库/测试用法）时决策照常完成——审计事件不阻断主链路。"""
        store = _store_with_pending(tmp_path, "s1")
        reply = await apply_decision(store, "s1", "allow_session", "bash")
        assert "approval_decision" in reply

    @pytest.mark.asyncio
    async def test_bus_routing_is_session_scoped(self, tmp_path: Path) -> None:
        """破坏者：两会话各绑一总线——s1 的决策不得发到 s2 的链（不串）。"""
        store = _store_with_pending(tmp_path, "s1")
        bus1, bus2 = EventBus(), EventBus()
        events1 = await _spy(bus1, EventType.APPROVAL_DENIED)
        events2 = await _spy(bus2, EventType.APPROVAL_DENIED)
        store.bind_event_bus(bus1, session_id="s1")
        store.bind_event_bus(bus2, session_id="s2")

        await apply_decision(store, "s1", "deny", "bash")

        assert len(events1) == 1
        assert len(events2) == 0, "跨会话事件串扰会污染审计链"
