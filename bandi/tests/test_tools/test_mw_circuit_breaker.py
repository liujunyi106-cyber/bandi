from __future__ import annotations

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.circuit_breaker_mw import CircuitBreakerMiddleware
from bandi.tools.pipeline import ToolContext

pytestmark = pytest.mark.asyncio


class TestCircuitBreakerMiddleware:
    async def test_threshold_opens_circuit(self):
        mw = CircuitBreakerMiddleware(threshold=2, recovery_timeout=60)
        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})

        r1 = await mw.before_execute(ctx, call)
        assert r1.allow is True
        r1 = await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        assert r1.success is False

        r2 = await mw.before_execute(ctx, call)
        assert r2.allow is True
        r2 = await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        assert r2.success is False

        r3 = await mw.before_execute(ctx, call)
        assert r3.allow is False
        assert "Circuit breaker open" in r3.reason

    async def test_half_open_recovers_on_success(self):
        mw = CircuitBreakerMiddleware(threshold=1, recovery_timeout=0.1)
        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})

        await mw.before_execute(ctx, call)
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )

        import asyncio

        await asyncio.sleep(0.15)

        r = await mw.before_execute(ctx, call)
        assert r.allow is True
        r = await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=True, output="ok")
        )
        assert r.success is True

        r = await mw.before_execute(ctx, call)
        assert r.allow is True

    async def test_per_tool_isolation(self):
        mw = CircuitBreakerMiddleware(threshold=1, recovery_timeout=60)
        ctx = ToolContext()

        bash_call = ToolCall(id="1", name="bash", arguments={})
        await mw.before_execute(ctx, bash_call)
        await mw.after_execute(
            ctx, bash_call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )

        read_call = ToolCall(id="2", name="read", arguments={})
        r = await mw.before_execute(ctx, read_call)
        assert r.allow is True

    async def test_retry_failures_not_counted(self):
        """重试期间的失败（ctx._retrying=True）不得计入熔断计数——
        否则 1 次原始失败 + N 次重试失败重复计数，阈值被缩小到 1/N。"""
        mw = CircuitBreakerMiddleware(threshold=3, recovery_timeout=60)
        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})

        # 原始失败（非重试）→ 计数 1
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        # 2 次重试失败（_retrying=True）→ 不计入
        ctx._retrying = True
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        ctx._retrying = False

        # 阈值 3：仅 1 次真实失败 + 2 次重试失败 → 不应熔断
        r = await mw.before_execute(ctx, call)
        assert r.allow is True, "重试失败不得计入熔断计数"

        # 再 2 次真实失败 → 达阈值 3 熔断
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        r = await mw.before_execute(ctx, call)
        assert r.allow is False, "真实失败达阈值后必须熔断"

    async def test_retry_success_resets_count(self):
        """回归：重试成功必须重置熔断计数（工具最终恢复）。"""
        mw = CircuitBreakerMiddleware(threshold=2, recovery_timeout=60)
        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})

        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        ctx._retrying = True
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=True, output="ok")
        )
        ctx._retrying = False

        r = await mw.before_execute(ctx, call)
        assert r.allow is True
        # 成功已重置计数：再失败 1 次不到阈值 2
        await mw.after_execute(
            ctx, call, ToolResult(tool_call_id="1", tool_name="bash", success=False, output="")
        )
        r = await mw.before_execute(ctx, call)
        assert r.allow is True, "重试成功后计数已重置，单次失败不应熔断"
