from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bandi.agent.base import CoTStepType, ExecutionContext, TerminalReason
from bandi.agent.states import on_executing, on_init, on_parsing, on_thinking
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
from bandi.llm.mock import MockProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def make_ctx(**overrides) -> ExecutionContext:
    messages = overrides.pop("messages", [])
    provider = overrides.pop("provider", MockProvider())
    config = overrides.pop("config", AgentConfig())
    bus = overrides.pop("bus", RecordingBus())
    ctx = ExecutionContext(messages=messages, provider=provider, config=config, bus=bus)
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def base_ctx(provider: MockProvider | None = None, **kw) -> ExecutionContext:
    return make_ctx(
        messages=[Message(role=MessageRole.USER, content="task")],
        provider=provider or MockProvider(),
        raw_input="task",
        **kw,
    )


# ── THOUGHT 步骤（非流式） ────────────────────────────────────────────


class TestThoughtSteps:
    @pytest.mark.asyncio
    async def test_records_thought_with_reasoning(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="answer", reasoning="先分析问题"))
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        await on_init(ctx)
        await on_thinking(ctx)
        assert ctx.cot_trace[-1].type == CoTStepType.THOUGHT
        assert ctx.cot_trace[-1].thought == "先分析问题"
        assert ctx.cot_trace[-1].step == 1

    @pytest.mark.asyncio
    async def test_step_increments_across_turns(self) -> None:
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="tool", tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})]
            )
        )
        provider.add_response(LLMResponse(content="done", reasoning="第二段思考"))
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        await on_init(ctx)
        await on_thinking(ctx)
        await on_thinking(ctx)
        assert ctx.cot_trace[0].step == 1
        assert ctx.cot_trace[1].step == 2
        assert ctx.cot_trace[1].thought == "第二段思考"

    @pytest.mark.asyncio
    async def test_empty_reasoning_still_records_thought(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="answer"))
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        await on_init(ctx)
        await on_thinking(ctx)
        assert ctx.cot_trace[-1].type == CoTStepType.THOUGHT
        assert ctx.cot_trace[-1].thought == ""


# ── THINKING 流式透传 ────────────────────────────────────────────────


class TestStreamingThinking:
    @pytest.mark.asyncio
    async def test_stream_accumulates_thinking_into_trace(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="answer", reasoning="流式思考过程"))
        ctx = base_ctx(provider)
        await on_init(ctx)
        await on_thinking(ctx)
        assert ctx.cot_trace[-1].type == CoTStepType.THOUGHT
        assert ctx.cot_trace[-1].thought == "流式思考过程"

    @pytest.mark.asyncio
    async def test_stream_pushes_thinking_delta_events(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="answer", reasoning="思考增量"))
        ctx = base_ctx(provider)
        await on_init(ctx)
        await on_thinking(ctx)
        thinking_events = [
            e
            for e in ctx.bus.events
            if e.type == EventType.LLM_STREAM_EVENT and e.data.get("type") == "thinking_delta"
        ]
        assert len(thinking_events) >= 1
        assert "".join(e.data.get("content", "") for e in thinking_events) == "思考增量"

    @pytest.mark.asyncio
    async def test_stream_without_thinking_no_delta(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="plain answer"))
        ctx = base_ctx(provider)
        await on_init(ctx)
        await on_thinking(ctx)
        thinking_events = [
            e
            for e in ctx.bus.events
            if e.type == EventType.LLM_STREAM_EVENT and e.data.get("type") == "thinking_delta"
        ]
        assert thinking_events == []


# ── 工具步骤 + 最终答案 ──────────────────────────────────────────────


class TestToolAndAnswerSteps:
    def _ctx_with_executor(self, results: list[ToolResult]) -> ExecutionContext:
        ctx = base_ctx(config=AgentConfig(stream_to_ui=False))
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value=results)
        ctx.tool_executor = executor
        ctx.current_tool_calls = [ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})]
        return ctx

    @pytest.mark.asyncio
    async def test_executing_records_tool_call_and_result(self) -> None:
        ctx = self._ctx_with_executor(
            [ToolResult(tool_call_id="c1", tool_name="bash", success=True, output="file list")]
        )
        await on_executing(ctx)
        types = [s.type for s in ctx.cot_trace]
        assert types == [CoTStepType.TOOL_CALL, CoTStepType.TOOL_RESULT]
        assert ctx.cot_trace[0].tool_name == "bash"
        assert ctx.cot_trace[1].observation == "file list"

    @pytest.mark.asyncio
    async def test_executing_records_error_step(self) -> None:
        ctx = self._ctx_with_executor(
            [
                ToolResult(
                    tool_call_id="c1", tool_name="bash", success=False, output="", error="boom"
                )
            ]
        )
        await on_executing(ctx)
        assert ctx.cot_trace[-1].type == CoTStepType.ERROR
        assert ctx.cot_trace[-1].observation == ""

    @pytest.mark.asyncio
    async def test_parsing_records_answer_with_n_backfilled(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="done"))
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        await on_init(ctx)
        await on_thinking(ctx)
        await on_parsing(ctx)
        assert ctx.cot_trace[-1].type == CoTStepType.ANSWER
        assert ctx.cot_trace[-1].n == len(ctx.cot_trace)


# ── 完整一轮工具循环 ─────────────────────────────────────────────────


class TestFullLoopTrace:
    @pytest.mark.asyncio
    async def test_full_loop_builds_ordered_trace(self) -> None:
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})],
            )
        )
        provider.add_response(LLMResponse(content="done", reasoning="收尾思考"))
        executor = AsyncMock()
        executor.execute = AsyncMock(
            return_value=[
                ToolResult(tool_call_id="c1", tool_name="bash", success=True, output="files")
            ]
        )
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        ctx.tool_executor = executor
        await on_init(ctx)
        await on_thinking(ctx)
        await on_executing(ctx)
        await on_thinking(ctx)
        await on_parsing(ctx)
        types = [s.type for s in ctx.cot_trace]
        assert types == [
            CoTStepType.THOUGHT,
            CoTStepType.TOOL_CALL,
            CoTStepType.TOOL_RESULT,
            CoTStepType.THOUGHT,
            CoTStepType.ANSWER,
        ]
        assert ctx.cot_trace[-1].n == 5
        assert ctx.terminal_reason == TerminalReason.COMPLETED

    @pytest.mark.asyncio
    async def test_reasoning_content_not_in_messages(self) -> None:
        provider = MockProvider()
        provider.add_response(LLMResponse(content="answer", reasoning="隐藏思考"))
        ctx = base_ctx(provider, config=AgentConfig(stream_to_ui=False))
        await on_init(ctx)
        await on_thinking(ctx)
        await on_parsing(ctx)
        message_text = "".join(m.content or "" for m in ctx.messages)
        assert "隐藏思考" not in message_text


# ── 流式合并 + adapter 解析 ──────────────────────────────────────────


class TestMergeAndAdapters:
    @pytest.mark.asyncio
    async def test_merge_stream_accumulates_thinking(self) -> None:
        from bandi.llm.streaming import merge_stream_events

        async def gen():
            yield StreamEvent(type=StreamEventType.THINKING_DELTA, thinking_delta="先")
            yield StreamEvent(type=StreamEventType.THINKING_DELTA, thinking_delta="分析")
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="答案")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        response = await merge_stream_events(gen())
        assert response.reasoning == "先分析"

    def test_openai_extract_reasoning_keys(self) -> None:
        from bandi.llm.adapters.openai_compat import _extract_reasoning

        assert _extract_reasoning({"reasoning_content": "r"}) == "r"
        assert _extract_reasoning({"thinking": "t"}) == "t"
        assert _extract_reasoning({"reasoning": "z"}) == "z"
        assert _extract_reasoning({"content": "x"}) == ""

    def test_anthropic_extract_thinking_block(self) -> None:
        from bandi.llm.adapters.anthropic import _extract_reasoning

        blocks = [{"type": "thinking", "thinking": "深度思考"}, {"type": "text", "text": "hi"}]
        assert _extract_reasoning(blocks) == "深度思考"
        assert _extract_reasoning([{"type": "text", "text": "hi"}]) == ""

    def test_google_parse_thought_parts(self) -> None:
        from bandi.llm.adapters.google import _parse_google_response

        data = {
            "candidates": [
                {
                    "content": {"parts": [{"thought": True, "text": "思考"}, {"text": "答案"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        }
        resp = _parse_google_response(data)
        assert resp.reasoning == "思考"
        assert resp.content == "答案"
        assert (
            _parse_google_response(
                {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "STOP"}]}
            ).reasoning
            == ""
        )
