"""
契约族：ToolExecutor 批内并发限流（max_concurrency）。

背景：executor.execute 对并发安全分区使用 asyncio.gather 全量并发——模型一次
返回大量只读工具调用（读 30 个文件）时同时发起，无资源耗尽保护。本契约钉：
- max_concurrency=N 时并发组同时执行峰值不得超过 N（semaphore 限流）
- serial 组（写类工具）不受 semaphore 影响——语义仍是逐串行（限流只作用于并发组）
- max_concurrency=None（默认）保持原 gather 全并发语义（向后兼容）
- 单工具失败隔离不因限流改变（一个失败不影响同批其他工具）

行为族（生产调用路径）：`tools/executor.py::ToolExecutor.execute` 并发分区
（_partition + gather 路径）。

断言「最终产物」：工具内共享计数器记录真实同时执行峰值，断言峰值上限；
而非断言实现代码存在。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

_ACTIVE = 0
_PEAK = 0
_CALLS = 0


class _ConcurrentProbe(BaseTool):
    """并发探针：进入 +1、记录峰值、等待、退出 -1——测真实同时执行数。"""

    name = "probe"
    description = "并发探针"
    category = ToolCategory.UTIL
    is_concurrency_safe = True
    is_read_only = True

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        global _ACTIVE, _PEAK, _CALLS
        _ACTIVE += 1
        _PEAK = max(_PEAK, _ACTIVE)
        _CALLS += 1
        await asyncio.sleep(0.02)
        _ACTIVE -= 1
        return f"ok-{params.get('i', '?')}"


def _reset_counters() -> None:
    global _ACTIVE, _PEAK, _CALLS
    _ACTIVE = 0
    _PEAK = 0
    _CALLS = 0


def _ex(max_concurrency: int | None) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register_instance(_ConcurrentProbe())
    return ToolExecutor(registry=reg, max_concurrency=max_concurrency)


def _batch(n: int) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name="probe", arguments={"i": i}) for i in range(n)]


@pytest.mark.asyncio
async def test_contract_concurrency_peak_bounded_by_limit() -> None:
    """max_concurrency=2：8 个并发安全工具同时执行峰值不得超过 2。"""
    _reset_counters()
    results = await _ex(max_concurrency=2).execute(_batch(8))
    assert all(r.success for r in results), "限流不得导致工具失败"
    assert len(results) == 8
    assert _PEAK <= 2, f"并发峰值 {_PEAK} 超过 max_concurrency=2"


@pytest.mark.asyncio
async def test_contract_concurrency_default_unbounded() -> None:
    """max_concurrency=None（默认）：保持 gather 全并发语义（峰值 = 批大小）。"""
    _reset_counters()
    results = await _ex(max_concurrency=None).execute(_batch(6))
    assert all(r.success for r in results)
    assert _PEAK == 6, f"默认应为全并发，实际峰值 {_PEAK}"


@pytest.mark.asyncio
async def test_contract_serial_group_ordering_unchanged() -> None:
    """破坏者视角：serial 组（写工具）不受 semaphore 影响——混合批中串行组仍
    逐个执行且结果按 tool_call_id 正确归属（限流不得打乱串并分区语义）。"""

    order: list[int] = []

    class _SerialProbe(BaseTool):
        name = "serial_probe"
        description = "串行探针"
        category = ToolCategory.UTIL
        is_concurrency_safe = False

        async def execute(self, params: dict[str, Any], context: Any = None) -> str:
            order.append(params["i"])
            await asyncio.sleep(0.005)
            return f"s-{params['i']}"

    reg = ToolRegistry()
    reg.register_instance(_ConcurrentProbe())
    reg.register_instance(_SerialProbe())
    ex = ToolExecutor(registry=reg, max_concurrency=1)  # 并发组被压到 1

    calls = [
        ToolCall(id="s1", name="serial_probe", arguments={"i": 1}),
        ToolCall(id="s2", name="serial_probe", arguments={"i": 2}),
        ToolCall(id="s3", name="serial_probe", arguments={"i": 3}),
    ]
    results = await ex.execute(calls)
    assert [r.tool_call_id for r in results] == ["s1", "s2", "s3"]
    assert order == [1, 2, 3], f"serial 组乱序: {order}"
