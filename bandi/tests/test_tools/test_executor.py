from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _DummyTool:
    name: str = "dummy"
    description: str = "A dummy tool"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    is_read_only: bool = False
    tags: list = []
    cache_ttl: float = 0.0
    enabled: bool = True
    risk_level: str = "low"
    permissions_override: str | None = None
    parameters: dict = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }

    async def execute(self, params: dict, **kwargs: Any) -> str:
        return f"executed: {params.get('msg', '')}"

    def validate_params(self, params: dict) -> list[str]:
        if "msg" not in params:
            return ["missing msg"]
        return []

    def to_openai_schema(self) -> dict:
        return {}


class TestToolExecutor:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.registry = MagicMock(spec=ToolRegistry)
        self.executor = ToolExecutor(registry=self.registry)

    async def test_execute_tool_success(self) -> None:
        tool = _DummyTool()
        self.registry.get.return_value = tool

        calls = [ToolCall(id="1", name="dummy", arguments={"msg": "hello"})]
        results = await self.executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is True
        assert "hello" in results[0].output

    async def test_execute_tool_not_found(self) -> None:
        self.registry.get.return_value = None

        calls = [ToolCall(id="1", name="nonexistent", arguments={})]
        results = await self.executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is False
        assert "unknown tool" in results[0].error

    async def test_execute_tool_error(self) -> None:
        err_tool = MagicMock()
        err_tool.name = "broken"
        err_tool.is_concurrency_safe = True
        err_tool.cache_ttl = 0.0
        err_tool.permissions_override = None
        err_tool.validate_params.return_value = []
        err_tool.execute.side_effect = RuntimeError("something broke")
        self.registry.get.return_value = err_tool

        calls = [ToolCall(id="1", name="broken", arguments={})]
        results = await self.executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is False
        assert "something broke" in results[0].error

    async def test_concurrent_execution(self) -> None:
        tool = _DummyTool()
        self.registry.get.return_value = tool

        calls = [
            ToolCall(id="1", name="dummy", arguments={"msg": "a"}),
            ToolCall(id="2", name="dummy", arguments={"msg": "b"}),
        ]
        results = await self.executor.execute(calls)

        assert len(results) == 2
        assert all(r.success for r in results)
        # 并发调度/取消语义改由行为契约覆盖
        # （`test_contract_executor_cancellation.py`：取消组/对照组）。

    async def test_permission_denial_does_not_permanently_ban_tool(self) -> None:
        tool = _DummyTool()
        self.registry.get.return_value = tool
        pipeline = MagicMock()
        pipeline.check = AsyncMock(return_value=MagicMock(allow=False, reason="denied"))
        self.executor._permissions_pipeline = pipeline
        self.executor._consecutive_failure_threshold = 3

        calls = [ToolCall(id="1", name="dummy", arguments={"msg": "x"})]
        for _ in range(5):
            results = await self.executor.execute(calls)
            assert results[0].success is False
            assert "denied" in results[0].error

        assert self.executor._failure_count.get("dummy", 0) == 0

    async def test_duplicate_calls_in_batch_execute_once(self) -> None:
        tool = _DummyTool()
        self.registry.get.return_value = tool
        with patch.object(tool, "execute", wraps=tool.execute) as spy:
            calls = [
                ToolCall(id="1", name="dummy", arguments={"msg": "same"}),
                ToolCall(id="2", name="dummy", arguments={"msg": "same"}),
                ToolCall(id="3", name="dummy", arguments={"msg": "other"}),
            ]
            results = await self.executor.execute(calls)
        assert spy.await_count == 2
        assert len(results) == 3
        assert results[0].output == results[1].output
        assert results[0].tool_call_id == "1"
        assert results[1].tool_call_id == "2"
        assert results[0].output != results[2].output

    async def test_duplicate_calls_keep_distinct_tool_call_ids(self) -> None:
        tool = _DummyTool()
        self.registry.get.return_value = tool
        calls = [
            ToolCall(id="a1", name="dummy", arguments={"msg": "dup"}),
            ToolCall(id="b2", name="dummy", arguments={"msg": "dup"}),
        ]
        results = await self.executor.execute(calls)
        assert [r.tool_call_id for r in results] == ["a1", "b2"]
        assert results[0].output == results[1].output

    async def test_duplicate_failure_propagates_to_all_copies(self) -> None:
        err_tool = MagicMock()
        err_tool.name = "broken"
        err_tool.is_concurrency_safe = True
        err_tool.cache_ttl = 0.0
        err_tool.permissions_override = None
        err_tool.validate_params.return_value = []
        err_tool.execute.side_effect = RuntimeError("boom")
        self.registry.get.return_value = err_tool

        calls = [
            ToolCall(id="x1", name="broken", arguments={"k": 1}),
            ToolCall(id="x2", name="broken", arguments={"k": 1}),
        ]
        results = await self.executor.execute(calls)
        assert all(not r.success for r in results)
        assert [r.tool_call_id for r in results] == ["x1", "x2"]

    async def test_mixed_partition_results_are_not_misassigned(self) -> None:
        """
        结果必须按 tool_call_id 归属，不得张冠李戴（分区重排后索引错位）。"""
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _AllowAll:
            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                return True

        self.executor = ToolExecutor(
            registry=self.registry,
            permissions_pipeline=PermissionsPipeline(user_interaction=_AllowAll()),
        )
        serial_tool = _DummyTool()
        serial_tool.name = "bash"
        serial_tool.is_concurrency_safe = False
        serial_tool.validate_params = lambda p: []
        serial_tool.execute = AsyncMock(return_value="OUT-bash")

        concurrent_tool = _DummyTool()
        concurrent_tool.name = "read"
        concurrent_tool.is_concurrency_safe = True
        concurrent_tool.validate_params = lambda p: []
        concurrent_tool.execute = AsyncMock(return_value="OUT-read")

        def _get(name: str):
            return serial_tool if name == "bash" else concurrent_tool

        self.registry.get = MagicMock(side_effect=_get)
        calls = [
            ToolCall(id="c-bash", name="bash", arguments={"command": "ls"}),
            ToolCall(id="c-read", name="read", arguments={"file_path": "x.py"}),
        ]
        results = await self.executor.execute(calls)
        by_id = {r.tool_call_id: r.output for r in results}
        assert by_id["c-bash"] == "OUT-bash", f"bash 结果错配: {by_id}"
        assert by_id["c-read"] == "OUT-read", f"read 结果错配: {by_id}"

    async def test_cache_hit_normalizes_non_string_output(self) -> None:
        """
        缓存命中时 output 必须归一化为 str——dict/list 输出被直接透传时
        preview 切片（(output or "")[:200]）抛出 TypeError。"""
        from bandi.core.message import ToolResult
        from bandi.tools.cache import ToolResultCache
        from bandi.tools.registry import ToolRegistry

        registry = ToolRegistry()
        tool = _DummyTool()
        tool.tags = ["util"]
        tool.cache_ttl = 60.0
        registry.register_instance(tool)
        cache = ToolResultCache()
        executor = ToolExecutor(registry=registry, result_cache=cache)
        key = cache.build_key("dummy", {"msg": "x"}, scope=executor.cache_scope)
        cache.set(
            key,
            ToolResult(tool_call_id="c1", tool_name="dummy", success=True, output={"rows": [1, 2]}),
        )
        results = await executor.execute([ToolCall(id="c1", name="dummy", arguments={"msg": "x"})])
        assert results[0].success is True, f"缓存命中不得因 dict 输出崩溃: {results[0].error}"
        assert isinstance(results[0].output, str), "缓存命中输出必须归一化为 str"
        assert "{'rows':" in results[0].output, f"dict 应 str 化透传，实际: {results[0].output!r}"

    async def test_validate_params_failure_produces_failed_result(self) -> None:
        """
        参数校验失败必须打到 ToolResult(success=False) 而非静默执行——
        validate_params 返回错误仍执行 execute = 失败参数被放行（接线）。"""
        bad_tool = MagicMock()
        bad_tool.name = "nv"
        bad_tool.is_concurrency_safe = True
        bad_tool.cache_ttl = 0.0
        bad_tool.permissions_override = None
        bad_tool.validate_params.return_value = ["code: 'opsx' does not match '^[0-9]+$'"]
        self.registry.get.return_value = bad_tool

        results = await self.executor.execute(
            [ToolCall(id="v1", name="nv", arguments={"code": "opsx"})]
        )

        assert results[0].success is False
        assert "参数校验失败" in results[0].error
        assert results[0].output == ""
        bad_tool.execute.assert_not_called()

    async def test_mixed_partition_side_effect_duplicates_each_execute(self) -> None:
        """
        有副作用工具（bash）即使参数相同也必须各自执行——去重复用结果会丢真实执行
        （一次性命令被去重为返回同一份复用结果，交给不同调用方）。"""
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _AllowAll:
            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                return True

        self.executor = ToolExecutor(
            registry=self.registry,
            permissions_pipeline=PermissionsPipeline(user_interaction=_AllowAll()),
        )
        serial_tool = _DummyTool()
        serial_tool.name = "bash"
        serial_tool.is_concurrency_safe = False
        serial_tool.is_idempotent = False
        serial_tool.is_read_only = False
        serial_tool.validate_params = lambda p: []
        serial_tool.execute = AsyncMock(return_value="OUT-bash")

        self.registry.get = MagicMock(side_effect=lambda name: serial_tool)
        calls = [
            ToolCall(id="b1", name="bash", arguments={"command": "ls"}),
            ToolCall(id="b2", name="bash", arguments={"command": "ls"}),
        ]
        results = await self.executor.execute(calls)
        by_id = {r.tool_call_id: r.output for r in results}
        assert by_id["b1"] == "OUT-bash"
        assert by_id["b2"] == "OUT-bash"
        assert serial_tool.execute.await_count == 2, (
            "非幂等工具不得去重——相同参数调用必须各自执行一次"
        )
