"""契约族：嵌套 subagent 治理。

生产调用路径：
1. SubagentManager.spawn——子 Agent 派生（trace 继承 / 递归深度上限）
2. SubagentManager._run_subagent——执行（trace 关联 / 失败 checkpoint 清理）
3. _ToolCallLimiter——工具调用次数上限（包装 ToolExecutor）
4. SubagentManager.cancel_all——orphan 清理

断言「最终产物」：子 trace 关联父 / 超深度拒绝 / 超工具次数 MAX_TURNS /
cancel_all 清空 / 失败清理 checkpoint。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from bandi.agent.subagent import (
    SubagentConfig,
    SubagentManager,
    SubagentSession,
    _ToolCallLimiter,
)
from bandi.core.message import ToolCall, ToolResult


def _mk_manager() -> SubagentManager:
    return SubagentManager()


def test_trace_inherited_from_parent() -> None:
    """子 Agent 继承父 trace_id（parent_trace_id 透传）。"""
    from bandi.agent import subagent as sub_mod
    from bandi.observability.tracer import generate_trace_id

    manager = _mk_manager()
    parent_trace = "trace-parent-123"
    trace_used: list[str] = []

    # Monkeypatch generate_trace_id 记录调用；若透传正确则不应被调用
    orig_generate = generate_trace_id

    def tracked_generate() -> str:
        trace_used.append(orig_generate())
        return trace_used[-1]

    sub_mod.generate_trace_id = tracked_generate
    try:
        session = SubagentSession(
            session_id=uuid4().hex,
            config=SubagentConfig(max_turns=1, timeout_sec=10),
            depth=0,
            parent_trace_id=parent_trace,
        )
        result = asyncio.run(
            manager._run_subagent("task", session.config, session, parent_trace_id=parent_trace)
        )
        assert result.success is True, "子 Agent 执行应成功"
        assert len(trace_used) == 0, (
            f"parent_trace_id={parent_trace} 应直接透传，不应触发 generate_trace_id；"
            f"实际调用次数={len(trace_used)}"
        )
    finally:
        sub_mod.generate_trace_id = orig_generate


def test_trace_fallback_when_parent_none() -> None:
    """无父 trace 时生成新 trace_id（fallback 路径）。"""
    from bandi.agent import subagent as sub_mod
    from bandi.observability.tracer import generate_trace_id

    manager = _mk_manager()
    generated: list[str] = []

    orig_generate = generate_trace_id

    def tracked_generate() -> str:
        t = orig_generate()
        generated.append(t)
        return t

    sub_mod.generate_trace_id = tracked_generate
    try:
        session = SubagentSession(
            session_id=uuid4().hex,
            config=SubagentConfig(max_turns=1, timeout_sec=10),
            depth=0,
            parent_trace_id="",  # 无父 trace
        )
        result = asyncio.run(
            manager._run_subagent("task", session.config, session, parent_trace_id="")
        )
        assert result.success is True
        assert len(generated) >= 1, "fallback 路径必须调用 generate_trace_id"
    finally:
        sub_mod.generate_trace_id = orig_generate


def test_recursion_depth_limit() -> None:
    """超深度 spawn 拒绝（max_depth=1 时 depth=1 拒绝）。"""
    manager = _mk_manager()
    cfg = SubagentConfig(max_depth=1, max_turns=1, timeout_sec=10)

    async def run() -> Any:
        deep = await manager.spawn("deep", config=cfg, depth=1)
        return deep

    result = asyncio.run(run())
    assert result.success is False
    assert "depth limit" in result.summary


def test_tool_call_limiter_enforces_limit() -> None:
    """工具调用次数上限：超限返回失败 ToolResult 并设 MAX_TURNS。"""
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.llm.mock import MockProvider

    class Inner:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, calls: list[ToolCall], **kw: Any) -> list[ToolResult]:
            self.calls += 1
            return [ToolResult(tool_call_id="t1", tool_name="x", success=True, output="ok")]

    inner = Inner()
    limiter = _ToolCallLimiter(inner, max_tool_calls=2)
    ctx = ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(max_iterations=5),
        bus=EventBus(),
    )
    call = ToolCall(id="t1", name="x", arguments={})

    async def run() -> Any:
        r1 = await limiter.execute([call], ctx=ctx)
        r2 = await limiter.execute([call], ctx=ctx)
        r3 = await limiter.execute([call], ctx=ctx)  # 超限
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    assert r1[0].success is True and r2[0].success is True
    assert r3[0].success is False, "超限必须返回失败结果"
    assert ctx.terminal_reason == TerminalReason.MAX_TURNS, "超限必须设 MAX_TURNS 终态"


def test_cancel_all_clears_active() -> None:
    """cancel_all 清理全部活跃子 Agent。"""
    manager = _mk_manager()
    manager._active["a"] = SubagentSession(session_id="a")
    manager._active["b"] = SubagentSession(session_id="b")
    manager.cancel_all()
    assert manager.get_active() == {}


def test_subagent_tool_schema_exposes_governance_params() -> None:
    """SubAgentTool schema 暴露 max_tool_calls/max_depth（LLM 可生成）。"""
    from bandi.agent.subagent_tool import SubAgentTool

    tool = SubAgentTool()
    props = tool.parameters["properties"]
    assert "max_tool_calls" in props, "schema 必须暴露 max_tool_calls"
    assert "max_depth" in props, "schema 必须暴露 max_depth"


def test_subagent_tool_depth_inherits_contextvar(monkeypatch: Any) -> None:
    """SubAgentTool 从 contextvar 继承深度——主 Agent 0 → 子 1 → 孙 2。"""
    from bandi.agent import subagent as sub_mod
    from bandi.agent.subagent import SubagentResult
    from bandi.agent.subagent_tool import SubAgentTool

    captured: dict[str, Any] = {}

    async def fake_spawn(
        self: Any,
        task: str,
        config: Any,
        tool_registry: Any = None,
        parent_trace_id: str | None = None,
        depth: int = 0,
    ) -> SubagentResult:
        captured["depth"] = depth
        return SubagentResult(success=True, summary="ok")

    monkeypatch.setattr(sub_mod.SubagentManager, "spawn", fake_spawn)
    tool = SubAgentTool()
    # 主 Agent 深度 0
    assert sub_mod._SUBAGENT_DEPTH.get() == 0
    asyncio.run(tool.execute({"task": "t"}))
    assert captured["depth"] == 1, "主 Agent 派生深度必须为 1"
    # 子 Agent 执行中（深度 1）再派生 → 2
    token = sub_mod._SUBAGENT_DEPTH.set(1)
    try:
        asyncio.run(tool.execute({"task": "t2"}))
        assert captured["depth"] == 2, "嵌套派生深度必须 +1"
    finally:
        sub_mod._SUBAGENT_DEPTH.reset(token)


def test_subagent_manager_close_closes_checkpoint_store() -> None:
    """SubagentManager.close 关闭共享 CheckpointStore 连接（防泄漏）。"""
    from bandi.agent.checkpoint import CheckpointStore

    manager = _mk_manager()
    store = manager._loop_checkpoint_store
    assert isinstance(store, CheckpointStore)
    manager.close()
    import sqlite3

    try:
        store._db.execute("SELECT 1")
        closed = False
    except sqlite3.ProgrammingError:
        closed = True
    assert closed, "close 必须真正关闭 sqlite 连接"


def test_subagent_tool_closes_manager_after_execute(monkeypatch: Any) -> None:
    """SubAgentTool execute 后 close manager（防每次 subagent 调用泄漏连接）。"""
    from bandi.agent import subagent as sub_mod
    from bandi.agent.subagent import SubagentResult
    from bandi.agent.subagent_tool import SubAgentTool

    closed: list[bool] = []

    async def fake_spawn(
        self: Any,
        task: str,
        config: Any,
        tool_registry: Any = None,
        parent_trace_id: str | None = None,
        depth: int = 0,
    ) -> SubagentResult:
        return SubagentResult(success=True, summary="ok")

    monkeypatch.setattr(sub_mod.SubagentManager, "spawn", fake_spawn)
    orig_close = sub_mod.SubagentManager.close

    def tracked_close(self: Any) -> None:
        closed.append(True)
        orig_close(self)

    monkeypatch.setattr(sub_mod.SubagentManager, "close", tracked_close)
    tool = SubAgentTool()
    asyncio.run(tool.execute({"task": "t"}))
    assert closed == [True], "execute 后必须 close manager"


def test_failed_subagent_cleans_checkpoint(monkeypatch: Any) -> None:
    """子 Agent 失败（MODEL_ERROR 终态）清理其 checkpoint；成功不清理。"""
    from bandi.agent.base import TerminalReason
    from bandi.agent.checkpoint import Checkpoint, CheckpointStore

    manager = _mk_manager()
    store = CheckpointStore()
    manager._loop_checkpoint_store = store
    session_id = uuid4().hex
    store.save(
        Checkpoint(
            id=uuid4().hex,
            session_id=session_id,
            turn_index=1,
            state_json="{}",
            messages_json="",
            created_at="T00:00:00+00:00",
        )
    )
    assert store.list_by_session(session_id), "前置 checkpoint 必须存在"

    from bandi.agent import loop as loop_mod

    async def fake_loop_run(self, ctx: Any, **kw: Any) -> Any:
        return TerminalReason.MODEL_ERROR

    monkeypatch.setattr(loop_mod.AgentLoop, "run", fake_loop_run)

    session = SubagentSession(
        session_id=session_id, config=SubagentConfig(max_turns=1, timeout_sec=5)
    )
    result = asyncio.run(manager._run_subagent("task", session.config, session))
    assert result.success is False, "MODEL_ERROR 终态必须判失败"
    assert store.list_by_session(session_id) == [], "失败必须清理 checkpoint"

    # 成功路径不清理
    session2_id = uuid4().hex
    store.save(
        Checkpoint(
            id=uuid4().hex,
            session_id=session2_id,
            turn_index=1,
            state_json="{}",
            messages_json="",
            created_at="T00:00:00+00:00",
        )
    )

    async def fake_loop_run_ok(self, ctx: Any, **kw: Any) -> Any:
        return TerminalReason.COMPLETED

    monkeypatch.setattr(loop_mod.AgentLoop, "run", fake_loop_run_ok)
    session2 = SubagentSession(
        session_id=session2_id, config=SubagentConfig(max_turns=1, timeout_sec=5)
    )
    result2 = asyncio.run(manager._run_subagent("task", session2.config, session2))
    assert result2.success is True
    assert store.list_by_session(session2_id), "成功子 Agent 不得清理 checkpoint"
