"""
契约族：去重粒度声明 dedup_scope（残余 TMC-）。

背景：executor 去重固定为 turn 内 (tool_name, args_hash) 粒度，只读工具一律去重。
但「read-only ≠ 结果不变」——读外部状态（文件/DB/外部 API）的工具同 turn 内
两次调用，中间状态可能已变，复用旧结果会丢真实读取。

本契约钉的行为族（生产调用路径）：
- `tools/base.py::BaseTool.dedup_scope` 声明（默认 "turn"；"none" = 禁止去重）
- `tools/executor.py::_is_dedup_eligible`（去重资格判定）
- `tools/executor.py::execute` 去重主路径

断言「最终产物」：dedup_scope="none" 的只读工具同参数两次调用必须各自执行
（await_count==2）；默认 turn 粒度的只读工具同参数去重（await_count==1）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _NoDedupReadTool(BaseTool):
    name = "live_read"
    description = "读外部状态（结果会变）"
    category = ToolCategory.UTIL
    is_read_only = True
    dedup_scope = "none"

    async def execute(self, params: dict, context=None) -> str:  # type: ignore[override]
        return "ok"


class _DefaultReadTool(BaseTool):
    name = "stable_read"
    description = "纯函数只读"
    category = ToolCategory.UTIL
    is_read_only = True

    async def execute(self, params: dict, context=None) -> str:  # type: ignore[override]
        return "ok"


def _executor_with(tool: BaseTool) -> tuple[ToolExecutor, AsyncMock]:
    tool.execute = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    reg = ToolRegistry()
    reg.register_instance(tool)
    return ToolExecutor(registry=reg), tool.execute


def test_contract_dedup_scope_none_forces_execution() -> None:
    """dedup_scope="none" 的只读工具：同参数两次调用必须各自执行（不丢真实读取）。"""
    ex, execute = _executor_with(_NoDedupReadTool())
    asyncio.run(
        ex.execute(
            [
                ToolCall(id="a", name="live_read", arguments={"x": 1}),
                ToolCall(id="b", name="live_read", arguments={"x": 1}),
            ]
        )
    )
    assert execute.await_count == 2, (
        f"dedup_scope=none 不得去重——读外部状态工具复用旧结果会丢真实读取，await_count={execute.await_count}"
    )


def test_contract_dedup_scope_default_turn_dedupes() -> None:
    """默认 dedup_scope（turn 粒度）的只读工具：同参数两次调用去重为一次执行。"""
    ex, execute = _executor_with(_DefaultReadTool())
    asyncio.run(
        ex.execute(
            [
                ToolCall(id="a", name="stable_read", arguments={"x": 1}),
                ToolCall(id="b", name="stable_read", arguments={"x": 1}),
            ]
        )
    )
    assert execute.await_count == 1, f"默认 turn 粒度应去重，await_count={execute.await_count}"


def test_contract_dedup_scope_base_default_is_turn() -> None:
    """BaseTool.dedup_scope 默认值必须是 "turn"（现有行为向后兼容）。"""
    assert BaseTool.dedup_scope == "turn"
