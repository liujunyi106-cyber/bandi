"""
契约族：工具重试指数退避——抖动 + 上限封顶。

背景：工具层 RetryMiddleware 退避公式 `base * 2**(attempt+1)` 无抖动且无
max_delay 封顶——attempt 足够大时 delay 指数爆炸；并发重试可能形成雷鸣群。
本契约钉：
- 退避 delay 不得超过 max_delay（默认 ，对齐 llm/circuit_breaker RetryPolicy）
- jitter=True（默认）时 delay 带随机抖动（因子 0.5~1.0），打破同步重试
- jitter=False 时 delay 为确定性指数序列 `base * factor**attempt`（首次=base）

行为族（生产调用路径）：`tools/middleware/retry_mw.py::RetryMiddleware.after_execute`
重试循环 + 公共退避函数 `utils/backoff.compute_backoff_delay`（与
llm/circuit_breaker 共用单一实现，防双实现漂移）。

断言「最终产物」：mock sleep 收集真实传入的 delay 值序列，断言其范围/分布
而非断言实现代码存在。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.retry_mw import RetryMiddleware
from bandi.tools.pipeline import ToolContext, ToolPipeline


def _call(name: str = "t1") -> ToolCall:
    return ToolCall(id=f"id-{name}", name=name, arguments={})


class _ReadOnlyStub:
    """5.3 fail-closed 后：重试类契约需显式只读工具（无声明默认禁重试）。"""

    name = "t1"
    is_read_only = True
    is_idempotent = False


def _ctx() -> ToolContext:
    return ToolContext(extra={"tool": _ReadOnlyStub()})


class _FlakyRun:
    """计数型 run_tool——前 fail_n 次返回失败，之后成功。"""

    def __init__(self, fail_n: int = 3) -> None:
        self.calls = 0
        self._fail_n = fail_n

    async def __call__(self, ctx: ToolContext, call: ToolCall) -> ToolResult:
        self.calls += 1
        if self.calls <= self._fail_n:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error="server error",
            )
        return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


def _pipeline(max_retries: int = 2, base_delay: float = 1.0, **mw_kwargs: Any) -> ToolPipeline:
    p = ToolPipeline()
    p.add(RetryMiddleware(max_retries=max_retries, base_delay=base_delay, **mw_kwargs))
    return p


@pytest.mark.asyncio
async def test_contract_retry_delay_capped_at_max_delay() -> None:
    """退避 delay 必须封顶于 max_delay（默认 ）——attempt 增大不指数爆炸。"""
    delays: list[float] = []
    real_sleep = AsyncMock(side_effect=lambda d: delays.append(d))

    # base=10.0 × 8 次重试——无 cap 时 10*2^7=1280s，有 cap 时全部 ≤30
    with patch("bandi.tools.middleware.retry_mw.asyncio.sleep", real_sleep):
        p = _pipeline(max_retries=8, base_delay=10.0)
        result = await p.execute(_ctx(), _call(), _FlakyRun(fail_n=8))

    assert result.success is True, "8 次失败后第 9 次成功，结果应为成功"
    assert delays, "必须产生重试 sleep"
    assert all(0.0 < d <= 30.0 for d in delays), f"delay 超出 max_delay 封顶: {delays}"


@pytest.mark.asyncio
async def test_contract_retry_delay_jitter_randomizes() -> None:
    """jitter=True（默认）：delay = 指数值 × 抖动因子(0.5~1.0]，不得是确定序列。"""
    delays: list[float] = []
    real_sleep = AsyncMock(side_effect=lambda d: delays.append(d))

    with patch("bandi.tools.middleware.retry_mw.asyncio.sleep", real_sleep):
        p = _pipeline(max_retries=2, base_delay=1.0)
        await p.execute(_ctx(), _call(), _FlakyRun(fail_n=2))

    assert len(delays) == 2, f"重试次数不符: {delays}"
    fixed_delays: list[float] = []
    fixed_sleep = AsyncMock(side_effect=lambda d: fixed_delays.append(d))
    with (
        patch("bandi.tools.middleware.retry_mw.asyncio.sleep", fixed_sleep),
        patch("bandi.utils.backoff.random.random", return_value=0.0),
    ):
        p2 = _pipeline(max_retries=2, base_delay=1.0)
        await p2.execute(_ctx(), _call(), _FlakyRun(fail_n=2))
    assert fixed_delays == [0.5, 1.0], f"jitter 因子未生效，固定随机下 delay={fixed_delays}"


@pytest.mark.asyncio
async def test_contract_retry_delay_deterministic_when_jitter_disabled() -> None:
    """jitter=False：delay 为确定性指数序列 base * factor**attempt（首次=base）。"""
    delays: list[float] = []
    real_sleep = AsyncMock(side_effect=lambda d: delays.append(d))

    with patch("bandi.tools.middleware.retry_mw.asyncio.sleep", real_sleep):
        p = _pipeline(max_retries=2, base_delay=1.0, jitter=False)
        await p.execute(_ctx(), _call(), _FlakyRun(fail_n=2))

    assert delays == [1.0, 2.0], f"jitter=False 应为确定性 [1.0, 2.0]，实际 {delays}"
