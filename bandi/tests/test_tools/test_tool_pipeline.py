from __future__ import annotations

import asyncio

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import (
    MiddlewareResult,
    ToolContext,
    ToolMiddleware,
    ToolPipeline,
)

pytestmark = pytest.mark.asyncio


class _AllowMiddleware(ToolMiddleware):
    async def before_execute(self, ctx, call):
        return MiddlewareResult(allow=True)

    async def after_execute(self, ctx, call, result):
        return result


class _DenyMiddleware(ToolMiddleware):
    async def before_execute(self, ctx, call):
        return MiddlewareResult(allow=False, reason="denied by test")

    async def after_execute(self, ctx, call, result):
        return result


class _ModifyParamsMiddleware(ToolMiddleware):
    async def before_execute(self, ctx, call):
        return MiddlewareResult(allow=True, modified_params={"modified": True})

    async def after_execute(self, ctx, call, result):
        result.meta["after_chain_ran"] = True
        return result


async def _fake_executor(ctx, call):
    return ToolResult(
        tool_call_id=call.id,
        tool_name=call.name,
        success=True,
        output="done",
    )


class TestToolPipeline:
    async def test_normal_execution(self):
        pipeline = ToolPipeline()
        pipeline.add(_AllowMiddleware())
        ctx = ToolContext()
        call = ToolCall(id="1", name="test_tool", arguments={})
        result = await pipeline.execute(ctx, call, _fake_executor)
        assert result.success is True
        assert result.output == "done"

    async def test_deny_short_circuits(self):
        pipeline = ToolPipeline()
        pipeline.add(_DenyMiddleware())
        pipeline.add(_AllowMiddleware())
        ctx = ToolContext()
        call = ToolCall(id="1", name="test_tool", arguments={})
        result = await pipeline.execute(ctx, call, _fake_executor)
        assert result.success is False
        assert "denied by test" in result.error

    async def test_modified_params_passed_to_executor(self):
        pipeline = ToolPipeline()
        pipeline.add(_ModifyParamsMiddleware())

        async def capture_params(ctx, call):
            assert call.arguments.get("modified") is True
            return await _fake_executor(ctx, call)

        ctx = ToolContext()
        call = ToolCall(id="1", name="test_tool", arguments={"original": True})
        result = await pipeline.execute(ctx, call, capture_params)
        assert result.success is True

    async def test_after_chain_modifies_result(self):
        pipeline = ToolPipeline()
        pipeline.add(_ModifyParamsMiddleware())
        ctx = ToolContext()
        call = ToolCall(id="1", name="test_tool", arguments={})
        result = await pipeline.execute(ctx, call, _fake_executor)
        assert result.meta.get("after_chain_ran") is True


class TestRetrySkipsBeforeChain:
    async def test_retry_does_not_rerun_before_chain(self):
        from bandi.core.message import ToolCall, ToolResult
        from bandi.tools.pipeline import (
            MiddlewareResult,
            ToolContext,
            ToolMiddleware,
            ToolPipeline,
        )

        calls = {"before": 0, "after": 0}

        class CountingMw(ToolMiddleware):
            async def before_execute(self, ctx, call):
                calls["before"] += 1
                return MiddlewareResult(allow=True)

            async def after_execute(self, ctx, call, result):
                calls["after"] += 1
                return result

        pipeline = ToolPipeline()
        pipeline.add(CountingMw())

        async def flaky(ctx, call):
            if ctx.extra.get("attempt", 0) == 0:
                ctx.extra["attempt"] = 1
                return ToolResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    success=False,
                    output="",
                    error="boom",
                )
            return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")

        ctx = ToolContext()
        call = ToolCall(id="1", name="test_tool", arguments={})
        result = await pipeline.execute(ctx, call, flaky)
        assert result.success is False
        # 触发重试
        await ctx._pipeline_execute(ctx, call)
        assert calls["before"] == 1, f"before 被重跑 {calls['before']} 次"
        assert calls["after"] == 2, f"after 应重跑 {calls['after']} 次"


class TestPermissionMiddlewareIdentity:
    async def test_role_scope_passed_to_pipeline_check(self):
        from bandi.core.message import ToolCall
        from bandi.tools.middleware.permission_mw import PermissionMiddleware
        from bandi.tools.permissions_pipeline import PermissionsPipeline, PermissionVerdict

        captured = {}

        class FakePP(PermissionsPipeline):
            async def check(self, call, tool, mode, **kwargs):
                captured.update(kwargs)
                return PermissionVerdict(allow=True)

        ctx = ToolContext(role="admin", scope="workspace:2")
        mw = PermissionMiddleware(pipeline=FakePP())
        call = ToolCall(id="1", name="write", arguments={})
        result = await mw.before_execute(ctx, call)
        assert result.allow is True
        assert captured.get("role") == "admin", f"role 未透传: {captured}"
        assert captured.get("scope") == "workspace:2", f"scope 未透传: {captured}"

    async def test_deny_propagates_reason(self):
        from bandi.core.message import ToolCall
        from bandi.tools.middleware.permission_mw import PermissionMiddleware
        from bandi.tools.permissions_pipeline import PermissionVerdict

        class DenyPP:
            async def check(self, call, tool, mode, **kwargs):
                return PermissionVerdict(allow=False, reason="role restricted")

        ctx = ToolContext(role="readonly", scope="default")
        mw = PermissionMiddleware(pipeline=DenyPP())
        call = ToolCall(id="1", name="bash", arguments={})
        result = await mw.before_execute(ctx, call)
        assert result.allow is False
        assert result.reason == "role restricted"


class TestConcurrentExecutionsIsolated:
    async def test_concurrent_executes_do_not_share_run_tool(self):
        """并发执行同一 pipeline 时，各协程必须使用自己的 run_tool，不得互相覆盖。"""

        class YieldingMw(ToolMiddleware):
            async def before_execute(self, ctx, call):
                await asyncio.sleep(0)
                return MiddlewareResult(allow=True)

            async def after_execute(self, ctx, call, result):
                return result

        pipeline = ToolPipeline()
        pipeline.add(YieldingMw())

        async def run_with(label: str, call_id: str) -> ToolResult:
            async def my_executor(_ctx, _call):
                return ToolResult(
                    tool_call_id=_call.id,
                    tool_name=label,
                    success=True,
                    output=f"executor-{label}",
                )

            ctx = ToolContext()
            call = ToolCall(id=call_id, name=f"tool-{label}", arguments={})
            return await pipeline.execute(ctx, call, my_executor)

        results = await asyncio.gather(run_with("A", "a1"), run_with("B", "b1"))
        outputs = {r.tool_call_id: (r.tool_name, r.output) for r in results}
        assert outputs["a1"] == ("A", "executor-A"), f"A 被覆盖: {outputs}"
        assert outputs["b1"] == ("B", "executor-B"), f"B 被覆盖: {outputs}"
