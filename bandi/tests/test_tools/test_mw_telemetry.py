from __future__ import annotations

from unittest.mock import patch

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.telemetry_mw import TelemetryMiddleware
from bandi.tools.pipeline import ToolContext

pytestmark = pytest.mark.asyncio


class TestTelemetryMiddleware:
    async def test_before_execute_records_metrics(self) -> None:
        mw = TelemetryMiddleware()
        ctx = ToolContext()
        call = ToolCall(id="1", name="bash", arguments={})

        with patch("time.monotonic", return_value=12345.0):
            result = await mw.before_execute(ctx, call)

        assert result.allow is True
        assert ctx.start_time == 12345.0

    async def test_after_execute_records_duration(self) -> None:
        mw = TelemetryMiddleware()
        ctx = ToolContext()
        ctx.start_time = 1000.0
        call = ToolCall(id="1", name="bash", arguments={})
        tool_result = ToolResult(
            tool_call_id="1", tool_name="bash", success=True, output="ok", duration_ms=0.0
        )

        with patch("time.monotonic", return_value=1500.0):
            updated = await mw.after_execute(ctx, call, tool_result)

        assert updated.duration_ms == 500000.0
        assert updated.success is True
        assert updated.output == "ok"
