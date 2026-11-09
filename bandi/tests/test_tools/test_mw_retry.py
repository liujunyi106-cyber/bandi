from __future__ import annotations

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.retry_mw import RetryMiddleware
from bandi.tools.pipeline import ToolContext, ToolPipeline

pytestmark = pytest.mark.asyncio


class _ReadOnlyStub:
    """5.3 fail-closed 后：重试测试需显式只读工具（无法证明安全默认禁重试）。"""

    name = "read_op"
    is_read_only = True
    is_idempotent = False


class _FakeExecutor:
    def __init__(self) -> None:
        self.call_count = 0
        self.fail_first_n = 0

    async def __call__(self, ctx: ToolContext, call: ToolCall) -> ToolResult:
        self.call_count += 1
        if self.call_count <= self.fail_first_n:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error="server error",
            )
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            success=True,
            output="ok",
        )


class TestRetryMiddleware:
    async def test_retry_succeeds_after_failure(self) -> None:
        pipeline = ToolPipeline()
        pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.01))

        executor = _FakeExecutor()
        executor.fail_first_n = 1

        ctx = ToolContext(extra={"tool": _ReadOnlyStub()})
        call = ToolCall(id="1", name="bash", arguments={})
        result = await pipeline.execute(ctx, call, executor)
        assert result.success is True
        assert executor.call_count == 2

    async def test_all_retries_fail(self) -> None:
        pipeline = ToolPipeline()
        pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.01))

        executor = _FakeExecutor()
        executor.fail_first_n = 10

        ctx = ToolContext(extra={"tool": _ReadOnlyStub()})
        call = ToolCall(id="1", name="bash", arguments={})
        result = await pipeline.execute(ctx, call, executor)
        assert result.success is False
        assert executor.call_count == 3

    async def test_non_retryable_error_skips_retry(self) -> None:
        pipeline = ToolPipeline()
        pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.01))

        async def fail_with_non_retryable(ctx: ToolContext, call: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error="validation error",
            )

        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})
        result = await pipeline.execute(ctx, call, fail_with_non_retryable)
        assert result.success is False
