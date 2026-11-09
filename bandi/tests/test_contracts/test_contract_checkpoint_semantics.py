"""
   写方：loop.py _run_loop 三类挂载点——
     a. 工具执行前（进入 executing 且 handler 未跑）→ 强制落盘（不受 interval 门控）
     b. RecoveryAction.RETRY 分支 → 强制落盘（dsh 事务协议：副作用/跳转前持久化）
     c. handler 成功后 → interval 门控（常规轮后保存，现有行为保持）
   读方：恢复链（agent_runner 续跑/checkpoint.load）以最近 turn_index 快照为准。

破坏者断言：interval=100（常规门控在第 2 轮必然跳过）时，工具 executor 执行
   现场 store 中必须已存在本会话第 2 轮的 checkpoint——没有 hook 即无盘
   崩溃重放将重复工具副作用（本次修复对象）。
"""

from typing import Any

import pytest


def _build_store(tmp_path: Any) -> Any:
    from bandi.agent.checkpoint import CheckpointStore

    return CheckpointStore(db_path=str(tmp_path / "ckpt.db"))


@pytest.mark.asyncio
async def test_checkpoint_exists_before_tool_execution_regardless_of_interval(
    tmp_path: Any,
) -> None:
    """
    interval=100 使常规轮后保存第 2 轮必然跳过，仍须有盘 = 强制 hook 存在的证据。
    """
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
    from bandi.llm.mock import MockProvider

    store = _build_store(tmp_path)
    provider = MockProvider()
    # 双工具轮：轮 1 在 interval 门控例外（turns_taken==1 全放行）内，轮 2
    # （2%100≠0）常规点必然跳过——executor 第 2 次现场只能靠强制 hook 有盘
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            finish_reason="tool_calls",
        )
    )
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c2", name="echo", arguments={})],
            finish_reason="tool_calls",
        )
    )
    provider.add_response(LLMResponse(content="收尾回答", tool_calls=[]))

    # 第 2 轮工具执行现场：store 必须已含 turn_index >= 2 记录
    second_call_rows: list[list[dict]] = []

    class _Exec:
        calls = 0

        async def execute(self, tool_calls: list[Any]) -> list[Any]:
            _Exec.calls += 1
            rows = store.list_by_session("sess-p15")
            if _Exec.calls >= 2:
                second_call_rows.append(list(rows))
                if not any(r["turn_index"] >= 2 for r in rows):
                    raise AssertionError(
                        f"第 2 轮工具执行前必须已有本轮 checkpoint（实际 turn 集合"
                        f" {[r['turn_index'] for r in rows]}）——interval 门控轮的"
                        f"副作用无保护，崩溃重放将重复工具执行"
                    )
            return [
                ToolResult(tool_call_id=t.id, tool_name=t.name, success=True, output="ok")
                for t in tool_calls
            ]

    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="任务"),
        ],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.meta["session_id"] = "sess-p15"
    ctx.tool_executor = _Exec()

    reason = await AgentLoop(max_iterations=6, checkpoint_store=store, checkpoint_interval=100).run(
        ctx
    )

    assert reason is TerminalReason.COMPLETED
    assert second_call_rows and second_call_rows[0], "第 2 轮 executor 现场必须读到 checkpoint 列表"


@pytest.mark.asyncio
async def test_injection_boundary_persists_before_replay(tmp_path: Any) -> None:
    """
    ——钉住「注入消费前必有恢复点」不变式。快照含注入消息（append 先于落盘）：
    恢复重放自带新指令且 clear_injection 幂等，不会二次注入；原始消息另有
    session store 持久化兜底。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType
    from bandi.core.message import LLMResponse, Message, MessageRole
    from bandi.llm.mock import MockProvider

    store = _build_store(tmp_path)
    provider = MockProvider()
    provider.add_response(LLMResponse(content="回应一"))
    provider.add_response(LLMResponse(content="回应二"))

    empty_at_injection: list[int] = []

    async def watch(event: Event) -> None:
        if event.type == EventType.USER_INPUT_INJECTED:
            n = len(store.list_by_session("sess-inject"))
            empty_at_injection.append(n)

    bus = EventBus()
    await bus.subscribe(EventType.USER_INPUT_INJECTED, watch)

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="原始任务")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.meta["session_id"] = "sess-inject"
    ctx.injected_input = "中途转向"
    ctx.current_state = "observing"

    await AgentLoop(max_iterations=5, checkpoint_store=store, checkpoint_interval=100).run(ctx)

    assert empty_at_injection and all(n > 0 for n in empty_at_injection), (
        f"USER_INPUT_INJECTED 发出前必须已落盘（事件时刻 store 记录数 {empty_at_injection}）"
        f"——注入上下文突变前的原始状态必须可恢复"
    )
    assert len(store.list_by_session("sess-inject")) > 0


@pytest.mark.asyncio
async def test_retry_branch_still_persists(tmp_path: Any) -> None:
    from bandi.agent.base import ExecutionContext, RecoveryAction
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, Message, MessageRole

    store = _build_store(tmp_path)

    class _FlakyProvider:
        model_name = "mock-flaky"
        protocol = "openai"
        supports_streaming = False

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient llm failure")
            return LLMResponse(content="恢复后完成")

        def count_tokens(self, *args: Any, **kwargs: Any) -> int:
            return 5

    provider = _FlakyProvider()

    class _LenientRecovery:
        def __init__(self) -> None:
            self.handled = 0

        async def handle(self, exc: Exception, state: str) -> RecoveryAction:
            self.handled += 1
            if self.handled > 5:
                return RecoveryAction.TERMINATE  # 防失控：上限后终止而非无限 RETRY
            return RecoveryAction.RETRY

        def on_turn_completed(self) -> None:
            return None

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=provider,  # type: ignore[arg-type]
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.meta["session_id"] = "sess-retry"
    ctx.recovery_manager = _LenientRecovery()  # type: ignore[assignment]

    await AgentLoop(max_iterations=3, checkpoint_store=store, checkpoint_interval=0).run(ctx)

    assert provider.calls >= 2, "RETRY 必须重试过 LLM"
    assert len(store.list_by_session("sess-retry")) > 0, (
        "RETRY 分支必须在前置位置落盘（interval=0 关闭常规保存，余下只有强制点）"
    )


@pytest.mark.asyncio
async def test_interval_gate_still_applies_to_routine_saves(tmp_path: Any) -> None:
    """
    高 interval(50) 下第 2 轮的 thinking/parsing 后不得产生常规记录（turn_index==2
    的常规迭代点零写入）。现状语义：首轮（turns_taken==1）每个状态迭代成功都放行。"""
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
    from bandi.llm.mock import MockProvider

    store = _build_store(tmp_path)
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            finish_reason="tool_calls",
        )
    )
    provider.add_response(LLMResponse(content="收尾"))

    class _Exec:
        async def execute(self, tool_calls: list[Any]) -> list[Any]:
            return [
                ToolResult(tool_call_id=t.id, tool_name=t.name, success=True, output="ok")
                for t in tool_calls
            ]

    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="任务"),
        ],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.meta["session_id"] = "sess-gate"
    ctx.tool_executor = _Exec()

    reason = await AgentLoop(max_iterations=5, checkpoint_store=store, checkpoint_interval=50).run(
        ctx
    )

    assert reason is TerminalReason.COMPLETED
    rows = store.list_by_session("sess-gate")
    turn1 = [r for r in rows if r["turn_index"] == 1]
    turn2 = [r for r in rows if r["turn_index"] >= 2]
    # 必须已放行（≥1）；interval=50 下第 2 轮迭代点仅终态强制保存恰一条
    assert len(turn1) == 1, f"每 turn 唯一约束：turn1 恰一条（最新覆盖，实际 {len(turn1)}）"
    assert len(turn2) == 1, f"interval=50 下第 2 轮仅终态保存一条（实际 {turn2}）"
