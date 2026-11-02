# 功能：契约测试——ToolProgressEvent 流式工具进度
# 钉行为族：① 工具可 override stream() 产出中间进度；② 未 override 的工具走原 execute()
# ④ _subscribe_handler 绑定把 TOOL_PROGRESS 转发给 StreamHandler（前端时间线）。
# 契约只钉行为结果，不钉实现（不 grep 源码文本）——全部经生产主链路（真实 ToolExecutor
# + 真实 EventBus + 真实 _subscribe_handler）断言最终产物事件/回调。
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bandi.core.abort_controller import AbortController
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolPermissionContext, ToolProgressEvent
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _ConcreteTool(BaseTool):  # 可实例化的最小 BaseTool，用于默认 stream() 语义契约
    name = "concrete"
    description = "minimal"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        return f"ran:{params}"


class _StreamingTool:  # duck-typed override stream() 的工具（模拟支持增量进度的工具）
    name = "streamer"
    is_concurrency_safe = True
    is_idempotent = True
    is_read_only = False
    cache_ttl = 0.0
    enabled = True
    risk_level = "low"
    permissions_override = None
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, params: dict, **kwargs: Any) -> str:
        return "final-result"

    def validate_params(self, params: dict) -> list[str]:
        return []

    async def stream(self, params: dict, **kwargs: Any):
        yield ToolProgressEvent(type="status", data={"step": 1})
        yield ToolProgressEvent(type="output", data={"chunk": "partial"})
        yield ToolProgressEvent(type="complete", data={"result": await self.execute(params)})


class _OneShotNoStreamTool:  # 无 stream 属性的旧鸭子工具（零回归护栏）
    name = "oneshot"
    is_concurrency_safe = True
    is_idempotent = True
    is_read_only = False
    cache_ttl = 0.0
    enabled = True
    risk_level = "low"
    permissions_override = None
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, params: dict, **kwargs: Any) -> str:
        return f"oneshot:{params.get('msg', '')}"

    def validate_params(self, params: dict) -> list[str]:
        return []


class _StreamingCancellableTool:  # cancellable 流式工具（覆盖 abort-竞速生产路径）
    name = "streamable"
    is_concurrency_safe = True
    is_idempotent = True
    is_read_only = False
    cache_ttl = 0.0
    enabled = True
    risk_level = "low"
    permissions_override = None
    cancellable = True
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, params: dict, **kwargs: Any) -> str:
        return "cancel-safe-result"

    def validate_params(self, params: dict) -> list[str]:
        return []

    async def stream(self, params: dict, **kwargs: Any):
        yield ToolProgressEvent(type="status", data={"step": "begin"})
        yield ToolProgressEvent(type="complete", data={"result": await self.execute(params)})


class _RecorderHandler:  # StreamHandler 记录回调（协议实现）
    def __init__(self) -> None:
        self.progress: list[tuple[str, str, dict]] = []
        self.results: list[tuple[str, bool, str]] = []

    async def on_content_delta(self, session_id: str, delta: str) -> None: ...

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None: ...

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.results.append((tool_name, success, output_preview))

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        self.progress.append((tool_name, progress_type, data))

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None: ...

    async def on_error(self, session_id: str, error: str) -> None: ...


pytestmark = pytest.mark.asyncio


# ── ① 默认 stream()：delegate execute()，产出单个 complete 事件 ─────────────
async def test_default_stream_delegates_to_execute() -> None:
    tool = _ConcreteTool()
    out: list[ToolProgressEvent] = []
    async for ev in tool.stream({"x": 1}, None):
        out.append(ev)
    assert len(out) == 1
    assert out[0].type == "complete"
    assert out[0].data["result"] == "ran:{'x': 1}"


# ── ② 未 override stream 的工具（无 stream 属性）仍走 execute 原路径 ─────────
async def test_no_stream_tool_keeps_execute_path() -> None:
    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus)
    tool = _OneShotNoStreamTool()
    registry.get.return_value = tool

    calls = [ToolCall(id="1", name="oneshot", arguments={"msg": "hi"})]
    results = await executor.execute(calls)
    assert results[0].success is True
    assert results[0].output == "oneshot:hi"


# ── ③ override stream 的工具：中间进度以 TOOL_PROGRESS 发到 EventBus，结果取自 complete ──
async def test_stream_tool_emits_tool_progress_events() -> None:
    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus)
    tool = _StreamingTool()
    registry.get.return_value = tool

    progress: list[Event] = []

    async def _on_progress(event: Event) -> None:
        progress.append(event)

    await bus.subscribe(EventType.TOOL_PROGRESS, _on_progress)

    calls = [ToolCall(id="1", name="streamer", arguments={})]
    results = await executor.execute(calls)

    # 结果来自 complete 事件载荷（final-result）
    assert results[0].success is True
    assert results[0].output == "final-result"

    # 事件链：2 个中间进度（status/output）以 TOOL_PROGRESS 发射，携带 tool_name/类型/data
    types = [e.data["progress_type"] for e in progress]
    assert types == ["status", "output"]
    assert all(e.data["tool_name"] == "streamer" for e in progress)
    assert progress[0].data["data"] == {"step": 1}
    assert progress[1].data["data"] == {"chunk": "partial"}


# ── ④ _subscribe_handler 绑定：TOOL_PROGRESS 转发到 StreamHandler.on_tool_progress ──
async def test_handler_bindings_forward_tool_progress() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus)
    tool = _StreamingTool()
    registry.get.return_value = tool

    handler = _RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        calls = [ToolCall(id="1", name="streamer", arguments={})]
        await executor.execute(calls)
    finally:
        await _unsubscribe_handler(bus, bindings)

    # 前端 StreamHandler 逐条收到进度（工具时间线数据源）
    assert [p[1] for p in handler.progress] == ["status", "output"]
    assert all(p[0] == "streamer" for p in handler.progress)
    assert handler.progress[0][2] == {"step": 1}
    assert handler.progress[1][2] == {"chunk": "partial"}
    # 结果帧仍在 on_tool_result 送出（时间线的最终态）
    assert handler.results and handler.results[0][1] is True


# ── ⑤ 族级覆盖：cancellable 流式工具走 abort-竞速分支（第二条生产路径）同样发进度 ──
async def test_cancellable_stream_tool_abort_race_path_emits_progress() -> None:
    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus, abort_controller=AbortController())
    tool = _StreamingCancellableTool()
    registry.get.return_value = tool

    progress: list[Event] = []

    async def _on_progress(event: Event) -> None:
        progress.append(event)

    await bus.subscribe(EventType.TOOL_PROGRESS, _on_progress)

    calls = [ToolCall(id="1", name="streamable", arguments={})]
    results = await executor.execute(calls)

    # 走 create_task(_invoke_or_execute) 竞速分支后，结果与进度均正常
    assert results[0].success is True
    assert results[0].output == "cancel-safe-result"
    assert [e.data["progress_type"] for e in progress] == ["status"]
