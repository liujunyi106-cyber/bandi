# 功能：契约——executor 取消语义（并发批 should_abort 取消 in-flight；
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.abort_controller import AbortController
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _SlowTool(BaseTool):
    name: str = "slow"
    description: str = "慢工具（可被取消）"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = True

    def __init__(self) -> None:
        super().__init__()
        self.finished = False

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        try:
            await asyncio.sleep(0.5)
            self.finished = True
        except asyncio.CancelledError:
            self.finished = False
            raise
        return "slow-done"


class _NoopTool(BaseTool):
    name: str = "noop"
    description: str = "快速工具"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = True

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "noop-ok"


class _ProbeTool(BaseTool):
    name: str = "probe"
    description: str = "调用计数探针（串行）"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        self.calls += 1
        return "probe-ok"


class _SerialAbortTool(BaseTool):
    name: str = "sabort"
    description: str = "串行批中止源（靠预置硬撞，不实际执行）"
    category: ToolCategory = ToolCategory.UTIL
    is_concurrency_safe: bool = False

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "never-runs"


def _mk_executor(*tools: BaseTool, controller: AbortController | None = None) -> ToolExecutor:
    reg = ToolRegistry()
    for t in tools:
        reg.register_instance(t)
    return ToolExecutor(
        registry=reg,
        execution_timeout=15,
        abort_controller=controller,
    )


@pytest.mark.asyncio
async def test_concurrent_batch_cancels_inflight_on_abort() -> None:
    """契约：并发批中某工具触发 should_abort 时，in-flight 同批必须被
    取消（不等待自然完成）——防止中止后副作用/资源浪费。"""
    slow = _SlowTool()
    ex = _mk_executor(slow, _NoopTool())
    # 预置 noop 硬撞（连续失败阈值 + 暂停反复调用计数）→ 本次调用 should_abort
    ex._failure_count["noop"] = 3
    ex._block_count["noop"] = 2

    results = await ex.execute(
        [
            ToolCall(id="s1", name="slow", arguments={}),
            ToolCall(id="a1", name="noop", arguments={}),
        ]
    )

    await asyncio.sleep(0.6)  # 给「未被取消」的慢工具自然完成的时间窗
    assert slow.finished is False, "should_abort 后 in-flight 慢工具必须被取消（实际跑完）"
    slow_result = next(r for r in results if r.tool_call_id == "s1")
    assert slow_result.success is False, "被取消的工具结果必须标记失败"
    assert "cancel" in (slow_result.error or "").lower() or "中止" in (slow_result.error or "")


@pytest.mark.asyncio
async def test_concurrent_batch_waits_all_without_abort() -> None:
    """契约（对照组）：无 should_abort 时并发批等全部完成（不得误取消）。"""
    slow = _SlowTool()
    ex = _mk_executor(slow, _NoopTool())

    results = await ex.execute(
        [
            ToolCall(id="s1", name="slow", arguments={}),
            ToolCall(id="n1", name="noop", arguments={}),
        ]
    )

    assert slow.finished is True, "正常路径不得取消慢工具"
    assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_any_tool_not_started_after_abort() -> None:
    """契约：中断（abort）已触发时，非 cancellable 工具也不得启动。"""
    probe = _ProbeTool()
    controller = AbortController()
    controller.abort("user_interrupt")
    ex = _mk_executor(probe, controller=controller)

    results = await ex.execute([ToolCall(id="p1", name="probe", arguments={})])

    assert probe.calls == 0, "中断后不得启动任何工具（含非 cancellable）"
    assert results[0].success is False
    assert "aborted" in (results[0].error or "").lower()


@pytest.mark.asyncio
async def test_serial_batch_stops_after_abort() -> None:
    """契约：串行批内工具触发 should_abort 后，后续工具不得派发。"""
    probe = _ProbeTool()
    ex = _mk_executor(probe, _SerialAbortTool())
    ex._failure_count["sabort"] = 3
    ex._block_count["sabort"] = 2

    results = await ex.execute(
        [
            ToolCall(id="a1", name="sabort", arguments={}),
            ToolCall(id="p1", name="probe", arguments={}),
        ]
    )

    assert probe.calls == 0, "中止后串行批后续工具不得执行"
    probe_result = next(r for r in results if r.tool_call_id == "p1")
    assert probe_result.success is False
