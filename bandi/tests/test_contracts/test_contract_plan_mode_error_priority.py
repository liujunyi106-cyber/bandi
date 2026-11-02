# 功能：契约 — plan 模式不可用的报错优先于参数校验失败（形态 B 根治：模式闸门前置）
from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor


class _PlanBlockedTool:
    """参数校验必失败的工具桩（模拟 plan 模式下被调用但不该执行的写工具）。"""

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


def _executor(tool: Any, mode: str) -> ToolExecutor:
    registry = Mock()
    registry.get.return_value = tool
    executor = ToolExecutor(registry=registry)
    executor._mode = mode
    return executor


def _call() -> ToolCall:
    return ToolCall(id="tc-1", name="ontology_assert", arguments={})


class TestPlanModeErrorPriority:
    @pytest.mark.asyncio
    async def test_plan_mode_error_takes_priority_over_param_error(self) -> None:
        tool = _PlanBlockedTool()
        result = await _executor(tool, "plan")._execute_one(_call())
        assert result.success is False
        assert "not allowed in plan mode" in (result.error or "")
        assert "[失败引导]" in (result.error or "")
        assert "参数校验失败" not in (result.error or "")
        assert tool.executed is False

    @pytest.mark.asyncio
    async def test_plan_mode_valid_params_still_blocked(self) -> None:
        tool = _PlanBlockedTool()
        tool.validate_params = lambda params: []
        result = await _executor(tool, "plan")._execute_one(_call())
        assert result.success is False
        assert "not allowed in plan mode" in (result.error or "")

    @pytest.mark.asyncio
    async def test_normal_mode_param_error_unchanged(self) -> None:
        tool = _PlanBlockedTool()
        result = await _executor(tool, "execute")._execute_one(_call())
        assert result.success is False
        assert "参数校验失败" in (result.error or "")
        assert "not allowed in plan mode" not in (result.error or "")
        assert tool.executed is False

    @pytest.mark.asyncio
    async def test_error_guides_model_to_inform_user(self) -> None:
        tool = _PlanBlockedTool()
        result = await _executor(tool, "plan")._execute_one(_call())
        assert "向用户说明" in (result.error or "")
