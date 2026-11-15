# 功能：契约 — DNS 类瞬时网络错误的退避放大 + 重试进度事件（前端重试条数据源）
from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.retry_mw import RetryMiddleware
from bandi.tools.pipeline import ToolContext


def _ctx(tool: Any = None, event_bus: EventBus | None = None) -> ToolContext:
    extra: dict[str, Any] = {"tool_call_id": "tc-1"}
    if tool is not None:
        extra["tool"] = tool
    if event_bus is not None:
        extra["event_bus"] = event_bus
    return ToolContext(extra=extra)


def _result(error: str) -> ToolResult:
    return ToolResult(
        tool_call_id="tc-1", tool_name="web_search", success=False, output="", error=error
    )


def _call() -> ToolCall:
    return ToolCall(id="tc-1", name="web_search", arguments={"query": "x"})


class TestDnsRetryAmplification:
    def test_dns_error_gets_amplified_policy(self) -> None:
        mw = RetryMiddleware(max_retries=2, base_delay=0.5)
        policy = mw._resolve_retry_policy(_ctx(), _result("getaddrinfo failed: dns lookup"), False)
        assert policy is not None
        assert policy.retryable is True
        # 放大：次数 +2、起步退避 ≥1.5s（原 2×0.5≈1.5s 总量 → 单跳即 1.5s）
        assert policy.max_retries == 4
        assert policy.backoff_seconds >= 1.5

    def test_dns_keywords_variants(self) -> None:
        mw = RetryMiddleware(max_retries=2, base_delay=0.5)
        for error in (
            "Name or service not known",
            "Temporary failure in name resolution",
            "DNS server returned SERVFAIL",
            "name resolution timed out",
        ):
            policy = mw._resolve_retry_policy(_ctx(), _result(error), False)
            assert policy is not None and policy.max_retries == 4, error

    def test_dns_error_on_non_idempotent_tool_still_blocked(self) -> None:
        tool = Mock()
        tool.name = "bash"
        tool.retry_policy = None
        tool.retryable = None
        tool.is_destructive = False
        mw = RetryMiddleware(max_retries=2, base_delay=0.5)
        policy = mw._resolve_retry_policy(_ctx(tool), _result("getaddrinfo failed"), False)
        assert policy is None


class TestRetryProgressEvent:
    @pytest.mark.asyncio
    async def test_retry_emits_tool_progress_event(self) -> None:
        seen: list[Event] = []
        bus = EventBus()
        original_emit = bus.emit

        async def capture(event: Event) -> None:
            seen.append(event)
            await original_emit(event)

        bus.emit = capture  # type: ignore[method-assign]
        tool = Mock()
        tool.name = "web_search"
        tool.retry_policy = None
        tool.retryable = None
        tool.is_destructive = False
        tool.is_read_only = True

        mw = RetryMiddleware(max_retries=2, base_delay=0.0, jitter=False)
        calls = [
            ToolResult(
                tool_call_id="tc-1", tool_name="web_search", success=True, output="ok", error=""
            )
        ]

        async def pipeline_execute(_ctx: Any, _call: ToolCall) -> ToolResult:
            return calls.pop(0)

        ctx = _ctx(tool, bus)
        ctx._pipeline_execute = pipeline_execute  # type: ignore[assignment]
        await mw.after_execute(ctx, _call(), calls and _result("boom"))

        retry_events = [e for e in seen if e.type == EventType.TOOL_PROGRESS]
        assert len(retry_events) == 1
        data = retry_events[0].data
        assert data["progress_type"] == "retry"
        assert data["tool_call_id"] == "tc-1"
        assert data["data"]["attempt"] == 1
        assert data["data"]["max_retries"] == 2
        assert "delay_seconds" in data["data"]

    @pytest.mark.asyncio
    async def test_no_bus_no_crash(self) -> None:
        tool = Mock()
        tool.name = "web_search"
        tool.retry_policy = None
        tool.retryable = None
        tool.is_destructive = False
        tool.is_read_only = True
        mw = RetryMiddleware(max_retries=1, base_delay=0.0, jitter=False)
        calls = [
            ToolResult(
                tool_call_id="tc-1", tool_name="web_search", success=True, output="ok", error=""
            )
        ]

        async def pipeline_execute(_ctx: Any, _call: ToolCall) -> ToolResult:
            return calls[0]

        ctx = _ctx(tool, None)
        ctx._pipeline_execute = pipeline_execute  # type: ignore[assignment]
        result = await mw.after_execute(ctx, _call(), _result("boom"))
        assert result.success is True
