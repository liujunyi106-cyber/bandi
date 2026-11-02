# 功能：契约——子代理嵌套过程：子代理事件带归属标签、父链转发为嵌套帧
# ProcessRecorder 落库成 task 节点 children；非子代理事件路径零回归。
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.process_recorder import ProcessRecorder


class _NullInner:
    async def on_tool_call_start(self, *a: Any, **k: Any) -> None: ...

    async def on_tool_result(self, *a: Any, **k: Any) -> None: ...

    async def on_task_ledger(self, *a: Any, **k: Any) -> None: ...


class TestSubagentEventBus:
    @pytest.mark.asyncio
    async def test_emit_injects_label_and_preserves_existing(self) -> None:
        from bandi.agent.subagent import _SubagentEventBus

        inner = EventBus()
        received: list[Event] = []

        async def _capture(event: Event) -> None:
            received.append(event)

        await inner.subscribe(EventType.TOOL_INVOKED, _capture)
        labeled = _SubagentEventBus(
            inner, {"id": "subagent-x", "parent_tool_call_id": "call_1", "task": "t"}
        )
        await labeled.emit(Event(type=EventType.TOOL_INVOKED, data={"tool_name": "bash"}))
        assert received[0].data["subagent"]["id"] == "subagent-x"

        preset = Event(
            type=EventType.TOOL_INVOKED,
            data={"tool_name": "read", "subagent": {"id": "keep"}},
        )
        await labeled.emit(preset)
        assert received[1].data["subagent"]["id"] == "keep", "已有标签不得被覆盖"


class _FakeHandler:
    def __init__(self, with_subagent_method: bool = True) -> None:
        self.subagent_calls: list[tuple[dict, str, dict]] = []
        self.parent_tool_starts: list[Any] = []
        if with_subagent_method:
            self.on_subagent_progress = self._on_subagent_progress  # type: ignore[attr-defined]

    async def _on_subagent_progress(
        self, label: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        self.subagent_calls.append((label, kind, payload))

    async def on_tool_call_start(self, *args: Any, **kwargs: Any) -> None:
        self.parent_tool_starts.append((args, kwargs))


LABEL = {"id": "subagent-x", "parent_tool_call_id": "call_1", "parent_session_id": "parent-s"}


class TestHandlerBindingsForwarding:
    @pytest.mark.asyncio
    async def test_subagent_event_forwarded_not_leaked_to_parent(self) -> None:
        from bandi.core.agent_runner import _subscribe_handler

        bus = EventBus()
        handler = _FakeHandler()
        await _subscribe_handler(bus, handler, "parent-s")
        await bus.emit(
            Event(
                type=EventType.TOOL_INVOKED,
                data={"tool_name": "bash", "tool_call_id": "c1", "subagent": dict(LABEL)},
            )
        )
        assert len(handler.subagent_calls) == 1
        label, kind, payload = handler.subagent_calls[0]
        assert label["id"] == "subagent-x"
        assert kind == "tool_start"
        assert payload["tool_name"] == "bash"
        assert handler.parent_tool_starts == [], "子代理事件不得泄漏进父过程流"

    @pytest.mark.asyncio
    async def test_other_session_label_not_forwarded(self) -> None:
        from bandi.core.agent_runner import _subscribe_handler

        bus = EventBus()
        handler = _FakeHandler()
        await _subscribe_handler(bus, handler, "parent-s")
        other = {**LABEL, "parent_session_id": "other-s"}
        await bus.emit(
            Event(type=EventType.TOOL_INVOKED, data={"tool_name": "bash", "subagent": other})
        )
        assert handler.subagent_calls == []

    @pytest.mark.asyncio
    async def test_parent_event_still_flows(self) -> None:
        from bandi.core.agent_runner import _subscribe_handler

        bus = EventBus()
        handler = _FakeHandler()
        await _subscribe_handler(bus, handler, "parent-s")
        await bus.emit(Event(type=EventType.TOOL_INVOKED, data={"tool_name": "read"}))
        assert len(handler.parent_tool_starts) == 1, "无标签事件必须走父路径（零回归）"

    @pytest.mark.asyncio
    async def test_handler_without_method_swallows_subagent_event(self) -> None:
        from bandi.core.agent_runner import _subscribe_handler

        bus = EventBus()
        handler = _FakeHandler(with_subagent_method=False)
        await _subscribe_handler(bus, handler, "parent-s")
        await bus.emit(
            Event(type=EventType.TOOL_INVOKED, data={"tool_name": "bash", "subagent": dict(LABEL)})
        )
        assert handler.parent_tool_starts == [], "不支持子代理通道的 handler 必须吞掉（不串台）"


class TestSpawnLabelsEventsEndToEnd:
    @pytest.mark.asyncio
    async def test_spawn_events_carry_label(self) -> None:
        from bandi.agent.subagent import SubagentConfig, SubagentManager

        bus = EventBus()
        captured: list[dict[str, Any]] = []

        async def _capture(event: Event) -> None:
            if isinstance(event.data, dict) and isinstance(event.data.get("subagent"), dict):
                captured.append(event.data["subagent"])

        await bus.subscribe(EventType.LLM_STREAM_EVENT, _capture)
        await bus.subscribe(EventType.TOOL_INVOKED, _capture)
        parent = SimpleNamespace(session_id="parent-s", bus=bus, meta={})
        manager = SubagentManager(parent_context=parent, event_bus=bus)
        await manager.spawn(
            "子任务：汇总资料",
            SubagentConfig(max_turns=1, timeout_sec=30),
            parent_tool_call_id="call_1",
        )
        assert captured, "子代理事件必须携带 subagent 标签（否则父链无法归属）"
        first = captured[0]
        assert first["parent_tool_call_id"] == "call_1"
        assert first["parent_session_id"] == "parent-s"
        assert str(first["id"]).startswith("subagent-")


def _read_process_metadata(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT metadata FROM session_entries WHERE entry_type = 'process'"
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


class TestProcessRecorderChildren:
    @pytest.mark.asyncio
    async def test_children_land_on_task_node_and_persist(self, tmp_path: Path) -> None:
        from bandi.memory.session.store import SessionStore

        db_path = tmp_path / "sessions.db"
        store = SessionStore(str(db_path))
        store.create_session("s1")

        async def _noop(*a: Any, **k: Any) -> None: ...

        inner = SimpleNamespace(
            on_tool_call_start=_noop,
            on_tool_result=_noop,
            on_thinking=_noop,
            on_task_ledger=_noop,
        )
        recorder = ProcessRecorder(inner, store, "s1")
        await recorder.on_tool_call_start("s1", "task", {"task": "调研"}, "call_1")
        label = {"id": "subagent-x", "parent_tool_call_id": "call_1", "parent_session_id": "s1"}
        await recorder.on_subagent_progress(label, "thinking", {"text": "先查资料。"})
        await recorder.on_subagent_progress(
            label, "tool_start", {"tool_name": "bash", "tool_call_id": "c9"}
        )
        await recorder.on_subagent_progress(
            label,
            "tool_result",
            {"tool_name": "bash", "success": True, "output_preview": "ok", "duration_ms": 5},
        )
        await recorder.persist_partial("s1", 1)

        metas = _read_process_metadata(db_path)
        assert len(metas) == 1
        nodes = metas[0]["process"]
        task_node = next(n for n in nodes if n.get("toolCallId") == "call_1")
        children = task_node.get("children") or []
        assert [c["kind"] for c in children] == ["thought", "tool"]
        assert children[1]["toolName"] == "bash"
        assert children[1]["status"] == "success"
        assert children[1]["detail"] == "ok"
        store.close()

    @pytest.mark.asyncio
    async def test_unmatched_parent_call_ignored(self, tmp_path: Path) -> None:
        from bandi.memory.session.store import SessionStore

        db_path = tmp_path / "sessions.db"
        store = SessionStore(str(db_path))
        store.create_session("s1")

        async def _noop(*a: Any, **k: Any) -> None: ...

        inner = SimpleNamespace(on_tool_call_start=_noop, on_tool_result=_noop, on_thinking=_noop)
        recorder = ProcessRecorder(inner, store, "s1")
        await recorder.on_tool_call_start("s1", "task", {}, "call_1")
        label = {
            "id": "subagent-y",
            "parent_tool_call_id": "no-such-call",
            "parent_session_id": "s1",
        }
        await recorder.on_subagent_progress(label, "tool_start", {"tool_name": "bash"})
        await recorder.persist_partial("s1", 1)

        metas = _read_process_metadata(db_path)
        task_node = next(n for n in metas[0]["process"] if n.get("toolCallId") == "call_1")
        assert not task_node.get("children"), "无匹配父节点的子代理事件必须忽略（不得挂错树）"
        store.close()


class TestSubagentToolWiring:
    """接线：SubAgentTool 必须把父会话/本调用 id 传给派生器（否则归属标签为空、嵌套不可达）。"""

    def test_tool_declares_needs_context(self) -> None:
        from bandi.agent.subagent_tool import SubAgentTool

        assert SubAgentTool().needs_context is True, (
            "session_id 经 needs_context 注入——未声明则 parent_session_id 恒空"
        )

    @pytest.mark.asyncio
    async def test_execute_passes_parent_session_and_call_id(self, monkeypatch: Any) -> None:
        from bandi.agent import subagent_tool as st_mod
        from bandi.agent.subagent import SubagentResult

        captured: dict[str, Any] = {}

        class _FakeManager:
            def __init__(self, **kwargs: Any) -> None:
                captured["manager"] = kwargs

            async def spawn_many(self, tasks: list[str], config: Any, **kwargs: Any) -> list[Any]:
                captured["spawn"] = kwargs
                return [
                    SubagentResult(
                        success=True, summary="ok", session_id="child-s", task_id="subagent-x"
                    )
                ]

            def close(self) -> None:
                captured["closed"] = True

        monkeypatch.setattr(st_mod, "SubagentManager", _FakeManager)
        tool = st_mod.SubAgentTool()
        ctx = SimpleNamespace(
            extra={"session_id": "sess-x", "tool_call_id": "call_1"}, event_bus=None
        )
        out = await tool.execute({"task": "调研"}, ctx)
        assert captured["manager"]["parent_context"].session_id == "sess-x", (
            "父会话身份必须传给派生器（嵌套帧归属依赖它）"
        )
        assert captured["spawn"]["parent_tool_call_id"] == "call_1"
        assert out["success"] is True
