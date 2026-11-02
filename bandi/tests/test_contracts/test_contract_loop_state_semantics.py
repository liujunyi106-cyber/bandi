"""
     reflecting 分支 should_retry() 先于 verdict_pass() 判定 → 已通过验证仍
     重回 thinking 多耗一轮空迭代）。生产路径 = on_reflecting(pass 分支) +
     should_retry()（transitions.py reflecting 迁移消费）双址——单清一边不足。
     生产路径 = _run_loop 注入分支 → on_thinking。
     生产路径 = loop.run() 入口钳制 + run_agent_turn 透传 + timeout 分支。

破坏者断言：rework_count>0 且已 pass 时 must next turn 不重回 thinking；重复
  pass 幂等；注入后 turn_index 与 turns_taken 严格差 1；负数 max_turns 拒绝。
"""

from types import SimpleNamespace
from typing import Any

import pytest


class _MockVerifier:
    """模拟 Verifier：verification_required=True，evaluate 返回给定 action。"""

    def __init__(self, action: str = "pass", reason: str = "") -> None:
        self._action = action
        self._reason = reason

    def verification_required(self, ctx: Any) -> bool:
        return True

    async def evaluate(self, packet: Any) -> Any:
        from bandi.agent.verifier import Verdict

        return Verdict(action=self._action, reason=self._reason)

    def apply_stagnation_cap(self, state: dict, verdict: Any) -> Any:
        return verdict


class _Bus:
    async def emit(self, *a: Any, **k: Any) -> None:
        return None


def _reflect_ctx(verifier: Any, rework_count: int = 0, rework_feedback: str = "") -> Any:
    """构造最小可跑 on_reflecting 的 ExecutionContext 形状。"""
    return SimpleNamespace(
        terminal_reason=None,
        final_message=None,
        raw_input="验证任务",
        messages=[],
        current_content="执行输出",
        tool_results=[],
        safety=SimpleNamespace(verifier=verifier),
        verifier_state=SimpleNamespace(rework_count=rework_count, rework_feedback=rework_feedback),
        meta={},
        turn_index=0,
        bus=_Bus(),
    )


@pytest.mark.asyncio
async def test_pass_after_rework_resets_rework_count() -> None:
    """
    为 False，不再陷入「已通过仍重回 thinking」的空迭代。"""
    from bandi.agent import states as states_mod

    verifier = _MockVerifier(action="pass", reason="这次正确")
    ctx = _reflect_ctx(verifier, rework_count=2, rework_feedback="上一轮需要返工")

    await states_mod.on_reflecting(ctx)

    assert ctx.terminal_reason is not None, "pass 后必须终止"
    assert ctx.verifier_state.rework_count == 0, (
        f"pass 后 rework_count 必须清零（实际 {ctx.verifier_state.rework_count}）"
    )
    # should_retry 双保险由 test_should_retry_false_when_terminal_reason_set
    # 对真实 ExecutionContext 断言——此处只验证 on_reflecting 产出的状态前提


@pytest.mark.asyncio
async def test_pass_after_rework_clears_feedback() -> None:
    from bandi.agent import states as states_mod

    verifier = _MockVerifier(action="pass")
    ctx = _reflect_ctx(verifier, rework_count=1, rework_feedback="残留的返工意见")

    await states_mod.on_reflecting(ctx)

    assert ctx.verifier_state.rework_feedback == "", (
        f"pass 后 rework_feedback 必须清空（实际 {ctx.verifier_state.rework_feedback!r}）"
    )


def test_should_retry_false_when_terminal_reason_set() -> None:
    """
    （pass→VERIFIED）should_retry() 必须为 False——双保险防 transitions 误判。"""
    from types import SimpleNamespace

    from bandi.agent.base import ExecutionContext, TerminalReason

    ctx = SimpleNamespace(
        terminal_reason=TerminalReason.VERIFIED,
        verifier_state=SimpleNamespace(rework_count=3, rework_feedback="x"),
    )
    # 通过 ExecutionContext 的真实 should_retry 逻辑验证（避免直接调用脆断言）
    assert ExecutionContext.should_retry(ctx) is False, "终止原因已设置时必须优先终止而非 retry"


# 状态机链 init→thinking→…→reflecting→(rework)→thinking 永不经 init，on_init 只在
# 每次新请求（新 artifact）被调度，故 init 清除 = 精准 artifact 边界；retry 链内经
# thinking 持续计数、pass 才清零，停滞封顶不受影响（由既有契约 test_pass_after_* 钉）。


@pytest.mark.asyncio
async def test_on_init_clears_stale_rework_for_new_artifact() -> None:
    """
    恢复的陈旧 rework_count/feedback 必须在新 artifact 的 init 被清零，否则
    should_retry() 会把新请求误送回返工。（破坏者：checkpoint 恢复形态的残留 count。）"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.states import on_init
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="新任务 B")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.verifier_state.rework_count = 3
    ctx.verifier_state.rework_feedback = "A 的陈旧返工意见"
    ctx.current_state = "init"
    ctx.raw_input = "新任务 B"

    await on_init(ctx)

    assert ctx.verifier_state.rework_count == 0, (
        f"新 artifact init 必须清除陈旧 rework_count（实际 {ctx.verifier_state.rework_count}）"
    )
    assert ctx.verifier_state.rework_feedback == "", (
        f"新 artifact init 必须清除陈旧 rework_feedback（实际 {ctx.verifier_state.rework_feedback!r}）"
    )
    assert ExecutionContext.should_retry(ctx) is False, "新 artifact 不得被陈旧 rework 误返工"


@pytest.mark.asyncio
async def test_injected_turn_advances_turn_index_once() -> None:
    """
    LLM_REQUEST 的 turn 序列必须从 1 起且无空洞（bug=注入分支与 on_thinking 双增）。"""
    from unittest.mock import AsyncMock, MagicMock

    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    mock_response = MagicMock()
    mock_response.content = "好的，处理注入"
    mock_response.tool_calls = []
    mock_response.finish_reason = "stop"
    provider.complete = AsyncMock(return_value=mock_response)
    provider.count_tokens = MagicMock(return_value=5)

    bus = EventBus()
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="原始任务"),
        ],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    # 建模：在 observing 阶段用户中途注入新指令
    ctx.current_state = "observing"
    ctx.injected_input = "中途转向：先看配置"

    llm_turns: list[int] = []

    async def capture(event: Event) -> None:
        if event.type == EventType.LLM_REQUEST:
            llm_turns.append(event.data.get("turn"))

    await bus.subscribe(EventType.LLM_REQUEST, capture)

    await AgentLoop(max_iterations=5).run(ctx)

    assert llm_turns, "至少一轮 LLM 请求"
    assert llm_turns == list(range(1, len(llm_turns) + 1)), (
        f"LLM_REQUEST turn 序列必须连续无空洞（实际 {llm_turns}）——"
        f"注入分支与 on_thinking 双增会使 turn 跳 2"
    )


# 行为族：loop.run() 的 max_turns 解析 `self._max_iterations or config.max_iterations`——
# 0/None 是「回落 config 默认」的有效哨兵，仅负数（truthy）与双 invalid 需兜底。


async def _run_until_max_turns(
    loop_max_iterations: int | None, config_max_iterations: int
) -> tuple[int, Any]:
    """铺开 AgentLoop 生产主链路跑至 MAX_TURNS，返回 (turn_index, reason)——
    最终产物断言（工具轮持续推动循环，MAX_TURNS 为唯一终止点）。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    for _ in range(40):
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            )
        )

    class _Exec:
        async def execute(self, tool_calls: list[Any]) -> list[Any]:
            return [
                ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
                for tc in tool_calls
            ]

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="go")],
        provider=provider,
        config=AgentConfig(max_iterations=config_max_iterations),
        bus=EventBus(),
    )
    ctx.tool_executor = _Exec()
    ctx.loop_detection_window = 50
    loop = AgentLoop(max_iterations=loop_max_iterations)
    reason = await loop.run(ctx)
    return ctx.turn_index, reason


@pytest.mark.asyncio
async def test_zero_max_iterations_falls_back_to_config() -> None:
    """
    0 是 run() 处「用 config」的有效哨兵。（回归点：旧修复钳 0→1 致 1 轮截断。）"""
    from bandi.agent.base import TerminalReason

    turn, reason = await _run_until_max_turns(loop_max_iterations=0, config_max_iterations=3)
    assert reason == TerminalReason.MAX_TURNS
    assert turn == 3, (
        f"max_iterations=0 必须恰好回落 config(3)（实际 {turn}）——"
        f"少于 3=被错误钳制，多于 3=越过 config 超配"
    )


@pytest.mark.asyncio
async def test_negative_max_iterations_falls_back_to_config() -> None:
    """
    负数为 truthy 会直穿 `or`，一级解析失败后仍须优先落实例配置而非类默认
    （回归点：首版返工负数直接跳类默认 15，放宽了用户显式上限）。"""
    from bandi.agent.base import TerminalReason

    turn, reason = await _run_until_max_turns(loop_max_iterations=-3, config_max_iterations=3)
    assert reason == TerminalReason.MAX_TURNS
    assert turn == 3, (
        f"负数 max_iterations 必须恰好回落 config(3)（实际 {turn}）——"
        f"1=首轮截断，15=跳过 config 超配"
    )


@pytest.mark.asyncio
async def test_positive_max_iterations_passthrough() -> None:
    from bandi.agent.base import TerminalReason

    turn, reason = await _run_until_max_turns(loop_max_iterations=2, config_max_iterations=5)
    assert reason == TerminalReason.MAX_TURNS
    assert turn == 2, f"正数 max_iterations=2 必须恰按 2 轮截断（实际 {turn}）"


@pytest.mark.asyncio
async def test_both_invalid_uses_sane_positive_default() -> None:
    """
    精确钉住兜底目标，破坏者视角：不得静默 0 立断 / 退化为 1 轮。"""
    from bandi.agent.base import TerminalReason
    from bandi.core.config import AgentConfig

    expected = AgentConfig().max_iterations
    turn, reason = await _run_until_max_turns(loop_max_iterations=0, config_max_iterations=0)
    assert reason == TerminalReason.MAX_TURNS
    assert turn == expected, (
        f"双 invalid 必须兜底类默认 {expected}（实际 {turn}）——不得 0 立断/1 轮退化"
    )
