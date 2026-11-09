# 功能：契约 — 参数校验失败的指令性追问引导与同签名失败阶梯（1 引导/2 升级/3 终止）
from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor


class _InvalidParamsTool:
    """参数校验必失败的工具桩。"""

    name = "ontology_assert"
    is_concurrency_safe = True
    external_content = False
    cache_ttl = 0
    permissions_override = None
    risk_level = "low"
    resource_type = "entity"

    def __init__(self) -> None:
        self.executed = False

    def validate_params(self, params: dict) -> list[str]:
        return ["缺少必填字段: entity_name"]

    async def execute(self, params: dict) -> str:
        self.executed = True
        return "SHOULD-NOT-RUN"


def _executor(tool: Any) -> ToolExecutor:
    registry = Mock()
    registry.get.return_value = tool
    return ToolExecutor(registry=registry)


def _call(arguments: dict | None = None) -> ToolCall:
    return ToolCall(id="tc-1", name="ontology_assert", arguments=arguments or {})


class TestParamValidationGuidance:
    @pytest.mark.asyncio
    async def test_first_failure_instructs_clarification(self) -> None:
        executor = _executor(_InvalidParamsTool())
        result = await executor._execute_one(_call())
        error = result.error or ""
        assert "参数校验失败" in error
        assert "禁止编造" in error
        assert "request_user_input" in error
        assert "失败终止" not in error

    @pytest.mark.asyncio
    async def test_second_failure_escalates(self) -> None:
        executor = _executor(_InvalidParamsTool())
        await executor._execute_one(_call())
        result = await executor._execute_one(_call())
        error = result.error or ""
        assert "第 2 次" in error
        assert "禁止再次以相同参数调用" in error
        assert "request_user_input" in error

    @pytest.mark.asyncio
    async def test_third_failure_terminates(self) -> None:
        tool = _InvalidParamsTool()
        executor = _executor(tool)
        await executor._execute_one(_call())
        await executor._execute_one(_call())
        result = await executor._execute_one(_call())
        error = result.error or ""
        assert "失败终止" in error
        assert "向用户说明" in error
        assert tool.executed is False

    @pytest.mark.asyncio
    async def test_different_args_count_independently(self) -> None:
        executor = _executor(_InvalidParamsTool())
        first = await executor._execute_one(_call({}))
        second = await executor._execute_one(_call({"entity_name": "x"}))
        assert "失败终止" not in (first.error or "")
        assert "失败终止" not in (second.error or "")
        assert "第 2 次" not in (second.error or "")

    @pytest.mark.asyncio
    async def test_ladder_resets_after_valid_params(self) -> None:
        tool = _InvalidParamsTool()
        executor = _executor(tool)
        await executor._execute_one(_call())
        await executor._execute_one(_call())
        tool.validate_params = lambda params: []
        passed = await executor._execute_one(_call())
        # 合法参数越过校验层（后续撞审批门属正常链路，不影响阶梯清零断言）
        assert "参数校验失败" not in (passed.error or "")
        tool.validate_params = lambda params: ["缺少必填字段: entity_name"]
        again = await executor._execute_one(_call())
        assert "失败终止" not in (again.error or "")
        assert "第 2 次" not in (again.error or "")
