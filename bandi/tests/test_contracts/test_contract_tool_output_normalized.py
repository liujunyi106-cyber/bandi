# 契约原则：工具结果进入消费路径（缓存读取回填 / 对话消息包裹 / CoT 观测 /
# working_store 预览）前，output 必须归一化为 str——dict/list 输出会打爆
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.core.message import Message, MessageRole, ToolCall, ToolResult
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.cache import ToolResultCache
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _DictOutputTool(BaseTool):
    name = "dict_op"
    description = "returns dict"
    category = ToolCategory.DOC
    cache_ttl = 60.0
    is_idempotent = True
    tags = ["doc"]

    async def execute(self, params: dict[str, Any], context=None):  # type: ignore[override]
        return {"rows": [1, 2], "summary": "ok"}


def _build_executor(cache: ToolResultCache) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(_DictOutputTool())
    return ToolExecutor(registry=registry, result_cache=cache)


def test_contract_cache_read_normalizes_non_string_output() -> None:
    """
    缓存读取路径（生产调用点：executor._cached_result）——直接 cache.set 非 str
    输出（绕过写路径归一化，模拟历史缓存/直接写入方），命中后回填必须为 str
    preview 切片不得抛 TypeError。"""
    cache = ToolResultCache()
    executor = _build_executor(cache)
    key = cache.build_key("dict_op", {"x": 1}, scope=executor.cache_scope)
    cache.set(
        key,
        ToolResult(tool_call_id="c1", tool_name="dict_op", success=True, output={"rows": [1]}),
    )
    result = asyncio.run(executor.execute([ToolCall(id="c1", name="dict_op", arguments={"x": 1})]))[
        0
    ]
    assert result.success is True, f"非 str 缓存命中不得崩溃: {result.error!r}"
    assert isinstance(result.output, str), "缓存命中回填输出必须归一化为 str"


@pytest.mark.asyncio
async def test_contract_cot_preview_normalizes_non_string() -> None:
    """
    CoT 观测预览路径（生产调用点：states.on_executing 工具结果 CoT 记录）——
    dict 输出做 [:N] 切片前必须 str 归一化，否则整轮执行崩溃。"""
    from bandi.agent import states as states_mod
    from bandi.agent.base import CoTStepType

    class _Exe:
        async def execute(self, calls):  # type: ignore[override]
            return [
                ToolResult(
                    tool_call_id="c1", tool_name="dict_op", success=True, output={"rows": [1, 2]}
                )
            ]

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="run")],
        provider=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        bus=None,  # type: ignore[arg-type]
    )
    ctx.current_tool_calls = [ToolCall(id="c1", name="dict_op", arguments={})]
    ctx.tool_results = []
    ctx.tool_executor = _Exe()  # type: ignore[attr-defined]
    ctx.turn_index = 1
    await states_mod.on_executing(ctx)
    obs = [s.observation for s in ctx.cot_trace if s.type == CoTStepType.TOOL_RESULT]
    assert obs and isinstance(obs[0], str), "dict 输出必须 str 化后进 CoT 观测"
    assert "'rows'" in obs[0]


def test_contract_working_store_preview_normalizes_non_string() -> None:
    """
    working_store 结果预览路径（生产调用点：agent_runner._persist_memory 工作现场持久化）——
    dict 输出做 [:200] 切片前必须 str 归一化。"""

    class _Store:
        def __init__(self) -> None:
            self.saved: list[dict] = []

        def append_tool(self, inv, **kw):
            self.saved.append(vars(inv))
            return None

    class _Manager:
        working = _Store()

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="run")],
        provider=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        bus=None,  # type: ignore[arg-type]
    )
    ctx.tool_results = [
        ToolResult(tool_call_id="c1", tool_name="dict_op", success=True, output={"rows": [1]}),
        ToolResult(tool_call_id="c2", tool_name="dict_op", success=True, output=[1, 2]),
    ]
    ctx.turn_index = 0
    ctx.raw_input = "run"
    from bandi.core.agent_runner import _persist_memory

    store = _Manager.working
    asyncio.run(_persist_memory(ctx, _Manager(), session_id="s1"))
    assert len(store.saved) == 2, "dict/list 输出不得让 working_store 持久化崩溃"
    assert all(isinstance(rec.get("result_preview"), str) for rec in store.saved)


def test_contract_tool_result_feeding_messages_normalizes_non_string() -> None:
    """
    工具结果注入对话路径（生产调用点：states._append_tool_results_to_messages）——
    dict 输出必须先 str 归一化再走 len/截断/XML 转义，否则 str.replace 抛
    AttributeError 整轮 thinking 崩掉。"""
    from bandi.agent.states import _append_tool_results_to_messages

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="run")],
        provider=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        bus=None,  # type: ignore[arg-type]
    )
    ctx.current_tool_calls = [ToolCall(id="c1", name="dict_op", arguments={})]
    ctx.tool_results = [
        ToolResult(tool_call_id="c1", tool_name="dict_op", success=True, output={"rows": [1, 2]})
    ]
    ctx.turn_index = 0
    _append_tool_results_to_messages(ctx)
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) == 1, "dict 输出必须正常注入对话上下文（不得崩溃）"
    assert "'rows'" in tool_msgs[0].content, "dict 输出必须 str 化后进入 tool_output 包裹"
