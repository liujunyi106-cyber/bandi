from __future__ import annotations

from unittest.mock import MagicMock

from bandi.agent.base import ExecutionContext, ParseOutcome, TerminalReason
from bandi.agent.transitions import build_default_machine
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall


class DummyProvider:
    supports_streaming = False

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(self, messages, tools=None, **kwargs):
        return LLMResponse(content="ok")

    async def stream(self, messages, tools=None, **kwargs):
        yield


def _ctx(**overrides) -> ExecutionContext:
    bus = MagicMock()
    bus.emit = MagicMock()
    messages = overrides.pop("messages", [Message(role=MessageRole.USER, content="test")])
    provider = overrides.pop("provider", DummyProvider())
    config = overrides.pop("config", AgentConfig(max_iterations=5))
    bus = overrides.pop("bus", bus)
    ctx = ExecutionContext(messages=messages, provider=provider, config=config, bus=bus)
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestBuildDefaultMachine:
    def test_all_states_registered(self) -> None:
        sm = build_default_machine()
        expected = {
            "init",
            "thinking",
            "parsing",
            "executing",
            "observing",
            "reflecting",
            "compressing",
            "terminated",
        }
        assert set(sm._transitions.keys()) == expected

    def test_terminated_has_no_transitions(self) -> None:
        sm = build_default_machine()
        assert sm._transitions["terminated"] == []

    def test_init_with_messages_goes_to_thinking(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx())
        assert next_state == "thinking"

    def test_init_without_messages_goes_to_terminated(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx(messages=[]))
        assert next_state == "terminated"

    def test_thinking_no_response_goes_to_terminated(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(
            _ctx(current_state="thinking", terminal_reason=TerminalReason.COMPLETED)
        )
        assert next_state == "terminated"

    def test_thinking_with_response_goes_to_parsing(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(
            _ctx(
                current_state="thinking",
                current_response=LLMResponse(content="ok"),
            )
        )
        assert next_state == "parsing"

    def test_parsing_with_tool_calls_goes_to_executing(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(
            _ctx(
                current_state="parsing",
                current_tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
                parse_outcome=ParseOutcome.PARSED,
            )
        )
        assert next_state == "executing"

    def test_parsing_without_tool_calls_goes_to_terminated(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="parsing"))
        assert next_state == "terminated"

    def test_parsing_terminal_reason_overrides_tool_calls(self) -> None:
        sm = build_default_machine()
        ctx = _ctx(
            current_state="parsing",
            current_tool_calls=[ToolCall(id="c1", name="bash", arguments={})],
            terminal_reason=TerminalReason.SAFETY_BLOCKED,
        )
        next_state = sm.next(ctx)
        assert next_state == "terminated"

    def test_executing_goes_to_observing(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="executing"))
        assert next_state == "observing"

    def test_observing_without_verifier_goes_to_thinking(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="observing"))
        assert next_state == "thinking"

    def test_observing_with_verifier_still_goes_to_thinking(self) -> None:
        """
        不得被 verifier 存在性短路（原挂点使单只读工具轮 VERIFIED 空终止）。"""
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="observing", verifier=object()))
        assert next_state == "thinking"

    def test_observing_with_terminal_reason_goes_terminated(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(
            _ctx(current_state="observing", terminal_reason=TerminalReason.AWAITING_APPROVAL)
        )
        assert next_state == "terminated"

    def test_compressing_under_budget_goes_thinking(self) -> None:
        """压缩已收敛（needs False）→ 回 thinking 正常续跑。"""
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="compressing"))
        assert next_state == "thinking"

    def test_compressing_still_over_budget_without_progress_goes_terminated(self) -> None:
        """压缩后仍超预算且**无成效**（缩不动）→ terminated——防压缩空转死循环
        （成分 A 护栏：无成效必须停下，不得无限重试）。"""
        sm = build_default_machine()
        ctx = _ctx(
            current_state="compressing",
            config=AgentConfig(max_iterations=5, context_window=100, compression_threshold=0.6),
            total_tokens=200,  # 无 count_tokens 时 compressible 回退 total_tokens
        )
        ctx.compress.last_shrunk = False
        next_state = sm.next(ctx)
        assert next_state == "terminated"

    def test_compressing_still_over_budget_with_progress_goes_thinking(self) -> None:
        """压缩后仍超预算但**本次确有缩减** → 回 thinking 续压再评估——否则巨大
        单条输入首轮单次压缩部分收敛即被 terminated → 兜底假 MAX_TURNS 空回复
        （冒烟 成分 A ：filler 单条 ~16K token，micro 不动 USER、progressive
        压一次未到阈值下，needs 仍 True → 旧转移直接 terminated）。turns 计数
        天然限制续压轮数。"""
        sm = build_default_machine()
        ctx = _ctx(
            current_state="compressing",
            config=AgentConfig(max_iterations=5, context_window=100, compression_threshold=0.6),
            total_tokens=200,
        )
        ctx.compress.last_shrunk = True
        next_state = sm.next(ctx)
        assert next_state == "thinking"

    def test_parsing_final_answer_goes_to_reflecting_when_verifier_set(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(_ctx(current_state="parsing", verifier=object()))
        assert next_state == "reflecting"

    def test_reflecting_goes_to_thinking_when_retry(self) -> None:
        sm = build_default_machine()
        ctx = _ctx(current_state="reflecting")
        ctx.verifier_state.rework_count = 1
        next_state = sm.next(ctx)
        assert next_state == "thinking"

    def test_reflecting_goes_to_terminated_when_terminal(self) -> None:
        sm = build_default_machine()
        next_state = sm.next(
            _ctx(current_state="reflecting", terminal_reason=TerminalReason.VERIFIED)
        )
        assert next_state == "terminated"

    def test_happy_path_state_sequence(self) -> None:
        sm = build_default_machine()
        ctx = _ctx(
            current_response=LLMResponse(content=""),
            current_tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
        )
        assert sm.next(ctx) == "thinking"
        ctx.current_state = "thinking"
        assert sm.next(ctx) == "parsing"
        ctx.current_state = "parsing"
        ctx.parse_outcome = ParseOutcome.PARSED
        assert sm.next(ctx) == "executing"
        ctx.current_state = "executing"
        assert sm.next(ctx) == "observing"
        ctx.current_state = "observing"
        assert sm.next(ctx) == "thinking"
