# 功能：工具中间件管线生效验证 — 限流拦截/重试真实工作

from __future__ import annotations

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import ToolContext, ToolPipeline


async def _ok_run_tool(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


@pytest.mark.asyncio
async def test_ratelimit_middleware_blocks_excess() -> None:
    """RateLimitMiddleware 超限必须拦截（before_execute 拒绝）。"""
    from bandi.tools.middleware.ratelimit_mw import RateLimitMiddleware, SlidingWindowRateLimiter

    pipeline = ToolPipeline()
    pipeline.add(RateLimitMiddleware(SlidingWindowRateLimiter(2, 60)))
    call = ToolCall(id="c1", name="read", arguments={})
    results = []
    for _ in range(4):
        results.append(await pipeline.execute(ToolContext(), call, _ok_run_tool))
    blocked = [r for r in results if not r.success]
    assert len(blocked) == 2, "第 3-4 次必须被限流拦截（限 2 次/窗）"
    assert results[0].success is True, "前 2 次应正常执行"


@pytest.mark.asyncio
async def test_retry_middleware_retries_transient_failures() -> None:
    """RetryMiddleware 必须对瞬时失败重试（run_tool 前 2 次失败第 3 次成功）。"""
    from bandi.tools.middleware.retry_mw import RetryMiddleware

    calls = {"n": 0}

    async def flaky_run(ctx: ToolContext, call: ToolCall) -> ToolResult:
        calls["n"] += 1
        if calls["n"] < 3:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error="transient",
            )
        return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=3, base_delay=0.01))
    call = ToolCall(id="c2", name="flaky", arguments={})

    class _ReadOnlyStub:
        name = "flaky"
        is_read_only = True
        is_idempotent = False

    result = await pipeline.execute(ToolContext(extra={"tool": _ReadOnlyStub()}), call, flaky_run)
    assert result.success is True
    assert calls["n"] >= 3, f"重试中间件必须重试，实际调用 {calls['n']} 次"


@pytest.mark.asyncio
async def test_circuit_breaker_middleware_opens_after_threshold() -> None:
    """CircuitBreakerMiddleware 连续失败达阈值后必须熔断（快速失败）。"""
    from bandi.tools.middleware.circuit_breaker_mw import CircuitBreakerMiddleware

    async def failing_run(ctx: ToolContext, call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            success=False,
            output="",
            error="boom",
        )

    pipeline = ToolPipeline()
    pipeline.add(CircuitBreakerMiddleware(threshold=3, recovery_timeout=60))
    call = ToolCall(id="c3", name="fail", arguments={})
    results = []
    for _ in range(6):
        results.append(await pipeline.execute(ToolContext(), call, failing_run))
    errors = [r.error for r in results if not r.success]
    assert any("circuit" in (e or "").lower() for e in errors), "熔断后必须快速失败（circuit open）"


@pytest.mark.asyncio
async def test_ownership_middleware_blocks_out_of_scope_write() -> None:
    """启用 ownership 并绑定 work_item（company 模式 + 写越界）时必须拦截。"""
    from bandi.agent.factory import build_tool_pipeline
    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItem, WorkItemTurnType
    from bandi.core.message import ToolCall

    work_item = WorkItem(
        id="wi-1",
        title="t",
        description="",
        phase=Phase.READY,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        parent_id=None,
        children_ids=[],
        links={},
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"write_scope": "read_only"},
        },
        runtime_metadata={},
    )
    pipeline = build_tool_pipeline(
        enable_ownership=True, work_item_provider=lambda _ctx, _call: work_item
    )
    call = ToolCall(id="c4", name="file_write", arguments={"file_path": "/tmp/out.txt"})
    result = await pipeline.execute(ToolContext(), call, _ok_run_tool)
    assert not result.success
    assert "Ownership" in (result.error or ""), f"越界写必须被 ownership 守卫拦截: {result.error}"


@pytest.mark.asyncio
async def test_ownership_disabled_by_default_no_block() -> None:
    """回归：默认（未启用 ownership）不得引入拦截（向后兼容）。"""
    from bandi.agent.factory import build_tool_pipeline
    from bandi.core.message import ToolCall

    pipeline = build_tool_pipeline()
    call = ToolCall(id="c5", name="file_write", arguments={"file_path": "/tmp/out.txt"})
    result = await pipeline.execute(ToolContext(), call, _ok_run_tool)
    assert result.success, "默认不启用 ownership 时不得拦截"
