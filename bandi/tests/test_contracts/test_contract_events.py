# 契约原则：不可序列化数据不中断分发链；event_id 幂等；memory_persist 稳定 event_id。
from __future__ import annotations

import asyncio

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType


def test_contract_unserializable_data_does_not_break_dispatch(tmp_path) -> None:
    """契约 1：事件 data 含不可序列化对象时持久化失败不得中断 handler 分发"""
    bus = EventBus(persist_path=tmp_path / "events.jsonl")
    bus.enable_persistence()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    async def _run() -> None:
        await bus.subscribe(EventType.LLM_RESPONSE, handler)

        class _Unserializable:
            pass

        await bus.emit(Event(type=EventType.LLM_RESPONSE, data={"obj": _Unserializable()}))
        await bus.close()

    asyncio.run(_run())
    assert len(received) == 1, "持久化失败不得阻断 handler 分发"


def test_contract_event_id_idempotent(tmp_path) -> None:
    from bandi.memory.global_store import GlobalMemoryStore

    store = GlobalMemoryStore(str(tmp_path / "g.db"))
    seq1 = store.append_event("E", {"n": 1}, event_id="evt-1")
    seq2 = store.append_event("E", {"n": 2}, event_id="evt-1")
    assert seq1 == seq2, "同 event_id 必须返回已有 seq（幂等）"
    events = store.query_events("E")
    assert len(events) == 1, "同 event_id 不得产生重复事件"


def test_contract_memory_persist_event_id_stable() -> None:
    """契约 3：memory_persist 事件用 (session, turn) 稳定 event_id——同会话同轮
    幂等（不重复），不同会话/不同轮共存（不互相覆盖）。机制修复：旧契约 grep
    源码断言 event_id 字符串存在，钉「代码存在」而非「行为结果」——字符串在但
    幂等语义被绕过照样绿。改为行为断言。"""
    from bandi.memory.global_store import GlobalMemoryStore

    store = GlobalMemoryStore(":memory:")
    seq1 = store.append_event("M", {"n": 1}, event_id="memory_persist:s1:1")
    seq2 = store.append_event("M", {"n": 2}, event_id="memory_persist:s1:1")
    seq3 = store.append_event("M", {"n": 3}, event_id="memory_persist:s1:2")
    seq4 = store.append_event("M", {"n": 4}, event_id="memory_persist:s2:1")
    assert seq1 == seq2, "同 (session,turn) 幂等不重复"
    assert seq3 != seq1, "不同 turn 必须新增"
    assert seq4 != seq1, "不同 session 必须新增"
    events = store.query_events("M")
    assert len(events) == 3, f"三个不同 (session,turn) 事件必须共存，实际 {len(events)}"
