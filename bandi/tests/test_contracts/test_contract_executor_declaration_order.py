# 功能：契约——工具批保声明序：连续同类工具分段、按声明序执行；
# 写后读批次必须读到写后值（不得把读全提到写之前并发执行）。
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _SharedState:
    def __init__(self) -> None:
        self.value = "old"
        self.write_done = False


class _WriteTool(BaseTool):
    name: str = "order_write"
    description: str = "串行写工具"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = False

    def __init__(self, state: _SharedState) -> None:
        super().__init__()
        self._state = state

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        await asyncio.sleep(0.05)
        self._state.value = "new"
        self._state.write_done = True
        return "written"


class _ReadTool(BaseTool):
    name: str = "order_read"
    description: str = "并发读工具"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = True

    def __init__(self, state: _SharedState) -> None:
        super().__init__()
        self._state = state
        self.calls = 0

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        self.calls += 1
        return f"value={self._state.value}"


def _mk_executor(*tools: BaseTool) -> ToolExecutor:
    reg = ToolRegistry()
    for t in tools:
        reg.register_instance(t)
    return ToolExecutor(registry=reg, execution_timeout=10)


@pytest.mark.asyncio
async def test_write_then_read_reads_new_value() -> None:
    """破坏者：声明序 [写, 读]——读必须看到写后的值。旧实现把读提前并发
    （先读旧值、后串行写）⇒ 读到 "value=old" 红。"""
    state = _SharedState()
    ex = _mk_executor(_WriteTool(state), _ReadTool(state))

    results = await ex.execute(
        [
            ToolCall(id="w1", name="order_write", arguments={}),
            ToolCall(id="r1", name="order_read", arguments={}),
        ]
    )

    read_result = next(r for r in results if r.tool_call_id == "r1")
    assert state.write_done is True
    assert read_result.output == "value=new", (
        f"声明序写后读必须读到写后值，实际 {read_result.output}"
    )


@pytest.mark.asyncio
async def test_read_then_write_reads_old_value() -> None:
    """对照组：声明序 [读, 写]——读先于写（旧实现同样通过，防过度收紧）。"""
    state = _SharedState()
    ex = _mk_executor(_ReadTool(state), _WriteTool(state))

    results = await ex.execute(
        [
            ToolCall(id="r1", name="order_read", arguments={}),
            ToolCall(id="w1", name="order_write", arguments={}),
        ]
    )

    read_result = next(r for r in results if r.tool_call_id == "r1")
    assert read_result.output == "value=old"
    assert state.write_done is True


@pytest.mark.asyncio
async def test_interleaved_segments_follow_declaration_order() -> None:
    """[读1, 读2, 写, 读3]：读3 必须看到写后的值；读1/读2 在写之前执行。"""
    state = _SharedState()
    ex = _mk_executor(_ReadTool(state), _WriteTool(state))

    results = await ex.execute(
        [
            ToolCall(id="r1", name="order_read", arguments={}),
            ToolCall(id="r2", name="order_read", arguments={}),
            ToolCall(id="w1", name="order_write", arguments={}),
            ToolCall(id="r3", name="order_read", arguments={}),
        ]
    )

    outs = {r.tool_call_id: r.output for r in results}
    assert outs["r1"] == "value=old"
    assert outs["r2"] == "value=old"
    assert outs["r3"] == "value=new"


class _SerialAbortTool(BaseTool):
    name: str = "order_abort"
    description: str = "中间串行段中止源（靠预置硬撞，不实际执行逻辑）"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = False

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "never-runs"


@pytest.mark.asyncio
async def test_middle_segment_abort_skips_later_segments() -> None:
    """中间段 should_abort → 后续并发段不得派发（跨段中止传播）。"""
    state = _SharedState()
    reader = _ReadTool(state)
    ex = _mk_executor(reader, _WriteTool(state), _SerialAbortTool())
    ex._failure_count["order_abort"] = 3
    ex._block_count["order_abort"] = 2

    results = await ex.execute(
        [
            ToolCall(id="r1", name="order_read", arguments={}),
            ToolCall(id="a1", name="order_abort", arguments={}),
            ToolCall(id="r2", name="order_read", arguments={}),
        ]
    )

    r2 = next(r for r in results if r.tool_call_id == "r2")
    assert r2.success is False
    assert "skipped" in (r2.error or "").lower() or "aborted" in (r2.error or "").lower()
    assert reader.calls == 1, "中止后后续并发段不得执行（读工具只被调用一次）"
