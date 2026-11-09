from __future__ import annotations

from bandi.agent.base import (
    STATE_NAMES,
    AgentState,
    ExecutionContext,
    RecoveryAction,
    StateMachine,
    TerminalReason,
    Transition,
)
from bandi.core.config import AgentConfig, LLMConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.llm.base import BaseProvider


class StubBus(EventBus):
    def __init__(self):
        super().__init__()

    async def emit(self, event):
        pass


class StubProvider(BaseProvider):
    def __init__(self):
        super().__init__(LLMConfig())

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(self, messages, tools=None, **kwargs):
        return LLMResponse(content="ok")

    async def stream(self, messages, tools=None, **kwargs):
        yield


def make_ctx(**overrides) -> ExecutionContext:
    messages = overrides.pop("messages", [])
    provider = overrides.pop("provider", StubProvider())
    config = overrides.pop("config", AgentConfig())
    bus = overrides.pop("bus", StubBus())
    ctx = ExecutionContext(messages=messages, provider=provider, config=config, bus=bus)
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestAgentState:
    def test_values(self) -> None:
        assert AgentState.INIT.value == "init"
        assert AgentState.THINKING.value == "thinking"
        assert AgentState.PARSING.value == "parsing"
        assert AgentState.EXECUTING.value == "executing"
        assert AgentState.OBSERVING.value == "observing"
        assert AgentState.REFLECTING.value == "reflecting"
        assert AgentState.COMPRESSING.value == "compressing"
        assert AgentState.TERMINATED.value == "terminated"

    def test_all_members(self) -> None:
        assert len(AgentState) == 8


class TestStateNames:
    def test_lookup(self) -> None:
        assert STATE_NAMES["init"] == AgentState.INIT
        assert STATE_NAMES["thinking"] == AgentState.THINKING
        assert STATE_NAMES["terminated"] == AgentState.TERMINATED

    def test_coverage(self) -> None:
        assert set(STATE_NAMES.keys()) == {s.value for s in AgentState}


class TestTerminalReason:
    def test_no_duplicate_values(self) -> None:
        values = [e.value for e in TerminalReason]
        assert len(values) == len(set(values))

    def test_count(self) -> None:
        assert len(TerminalReason) == 15


class TestRecoveryAction:
    def test_values(self) -> None:
        assert RecoveryAction.RETRY.value == 1
        assert RecoveryAction.TERMINATE.value == 2
        assert RecoveryAction.CONTINUE_DEGRADED.value == 3
        assert RecoveryAction.ESCALATE.value == 4

    def test_count(self) -> None:
        assert len(RecoveryAction) == 4


class TestTransition:
    def test_create_with_condition(self) -> None:
        t = Transition(target="thinking", condition=lambda ctx: True)
        assert t.target == "thinking"
        assert t.condition is not None
        assert t.condition(None) is True

    def test_create_without_condition(self) -> None:
        t = Transition(target="terminated")
        assert t.target == "terminated"
        assert t.condition is None


class TestExecutionContext:
    def test_needs_compression_below_threshold(self) -> None:
        ctx = make_ctx(total_tokens=1000)
        ctx.config.compression_threshold = 0.85
        ctx.config.context_window = 128000
        assert ctx.needs_compression() is False

    def test_needs_compression_above_threshold(self) -> None:
        ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="x" * 1000000)])
        ctx.config.compression_threshold = 0.85
        ctx.config.context_window = 128000
        assert ctx.needs_compression() is True

    def test_needs_compression_exact(self) -> None:
        ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="x" * 2000000)])
        ctx.config.compression_threshold = 0.85
        ctx.config.context_window = 128000
        assert ctx.needs_compression() is True

    def test_should_terminate_true(self) -> None:
        ctx = make_ctx(terminal_reason=TerminalReason.COMPLETED)
        assert ctx.should_terminate() is True

    def test_should_terminate_false(self) -> None:
        ctx = make_ctx(terminal_reason=None)
        assert ctx.should_terminate() is False

    def test_needs_verification_true(self) -> None:
        ctx = make_ctx(verifier=object())
        assert ctx.needs_verification() is True

    def test_needs_verification_false(self) -> None:
        ctx = make_ctx(verifier=None)
        assert ctx.needs_verification() is False

    def test_should_retry(self) -> None:
        ctx = make_ctx()
        assert ctx.should_retry() is False

    def test_verdict_pass_true(self) -> None:
        ctx = make_ctx(terminal_reason=TerminalReason.VERIFIED)
        assert ctx.verdict_pass() is True

    def test_verdict_pass_false(self) -> None:
        ctx = make_ctx(terminal_reason=TerminalReason.COMPLETED)
        assert ctx.verdict_pass() is False

    def test_should_escalate_true(self) -> None:
        ctx = make_ctx(terminal_reason=TerminalReason.ESCALATED)
        assert ctx.should_escalate() is True

    def test_should_escalate_false(self) -> None:
        ctx = make_ctx(terminal_reason=None)
        assert ctx.should_escalate() is False

    def test_compress_failed_below_threshold(self) -> None:
        ctx = make_ctx()
        ctx._compress_failed_count = 1
        assert ctx.compress_failed() is False

    def test_compress_failed_at_threshold(self) -> None:
        ctx = make_ctx()
        ctx._compress_failed_count = 2
        assert ctx.compress_failed() is True

    def test_compress_failed_above_threshold(self) -> None:
        ctx = make_ctx()
        ctx._compress_failed_count = 5
        assert ctx.compress_failed() is True

    def test_current_state_default(self) -> None:
        ctx = make_ctx()
        assert ctx.current_state == "init"

    def test_turn_index_starts_at_zero(self) -> None:
        ctx = make_ctx()
        assert ctx.turn_index == 0

    def test_total_tokens_starts_at_zero(self) -> None:
        ctx = make_ctx()
        assert ctx.total_tokens == 0

    def test_loop_detection_window(self) -> None:
        ctx = make_ctx(loop_detection_window=10)
        assert ctx.loop_detection_window == 10

    def test_zero_or_negative_window_falls_back(self) -> None:
        """五审：显式 0/负值不得让窗口变成 0（首轮即误杀）——回落默认 5。"""
        ctx = make_ctx(loop_detection_window=0)
        assert ctx.loop_detection_window == 5
        ctx2 = make_ctx(loop_detection_window=-3)
        assert ctx2.loop_detection_window == 5

    def test_meta_default_empty(self) -> None:
        ctx = make_ctx()
        assert ctx.meta == {}


class TestStateMachine:
    def test_register_and_next_condition_match(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="thinking", condition=lambda ctx: ctx.raw_input == "go"),
                Transition(target="terminated"),
            ],
        )
        ctx = make_ctx(raw_input="go")
        assert sm.next(ctx) == "thinking"

    def test_register_and_next_fallback(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="thinking", condition=lambda ctx: ctx.raw_input == "go"),
                Transition(target="terminated"),
            ],
        )
        ctx = make_ctx(raw_input="stop")
        assert sm.next(ctx) == "terminated"

    def test_next_terminated_stays_terminated(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="thinking"),
            ],
        )
        ctx = make_ctx(current_state="terminated")
        assert sm.next(ctx) == "terminated"

    def test_next_unregistered_state(self) -> None:
        sm = StateMachine()
        ctx = make_ctx(current_state="unknown")
        assert sm.next(ctx) == "terminated"

    def test_next_without_condition(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="thinking"),
            ],
        )
        ctx = make_ctx()
        assert sm.next(ctx) == "thinking"

    def test_no_matching_condition(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="thinking", condition=lambda ctx: False),
            ],
        )
        ctx = make_ctx()
        assert sm.next(ctx) == "terminated"

    def test_multiple_transitions_iterated_in_order(self) -> None:
        sm = StateMachine()
        sm.register(
            "init",
            [
                Transition(target="first", condition=lambda ctx: False),
                Transition(target="second", condition=lambda ctx: True),
                Transition(target="third"),
            ],
        )
        ctx = make_ctx()
        assert sm.next(ctx) == "second"
