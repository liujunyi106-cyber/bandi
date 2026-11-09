"""契约：非流式回退成功后必须补发流帧（空回复第二形态修复）。

背景：流式候选全败 → 静默降级非流式回退链 ⇒
聚合 content **只落库**（DB 有「2」）、从不产生 content_delta ⇒ UI 表现为
空回复（WS 零帧 + verdict_pass + completion_tokens>0）。
该表象曾被误判为「模型没回答」；留痕证明上游把答案放进了 reasoning 通道。

钉行为不钉实现：
- `stream_to_ui=True` 时，聚合响应必须补发 `thinking_delta` / `content_delta`；
- `stream_to_ui=False` 时必须**零帧**（不得无谓推流）；
- 过滤链（pii/sanitizer）异常不得阻断补发（空回复绝不能因过滤器崩掉而复现）。
"""

from __future__ import annotations

import pytest

from bandi.agent.states import _emit_aggregated_response_as_deltas
from bandi.core.config import AgentConfig
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, TokenUsage


class _Bus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def frames(self, frame_type: str) -> list[str]:
        return [
            str(e.data.get("content", ""))
            for e in self.events
            if e.type is EventType.LLM_STREAM_EVENT and e.data.get("type") == frame_type
        ]


class _Ctx:
    def __init__(self, *, stream_to_ui: bool, pii=None, sanitizer=None) -> None:
        self.config = AgentConfig(stream_to_ui=stream_to_ui)
        self.bus = _Bus()
        self.pii_detector = pii
        self.output_sanitizer = sanitizer


def _resp(content: str = "答案 2", reasoning: str = "思考链") -> LLMResponse:
    return LLMResponse(
        content=content,
        reasoning=reasoning,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=3),
        finish_reason="stop",
    )


class TestNonStreamFallbackEmitsDeltas:
    @pytest.mark.asyncio
    async def test_content_and_reasoning_both_emitted(self) -> None:
        """破坏者视角：把补发整段删掉（即回到修复前），本组必红。"""
        ctx = _Ctx(stream_to_ui=True)
        await _emit_aggregated_response_as_deltas(ctx, _resp())
        assert ctx.bus.frames("content_delta") == ["答案 2"], (
            "非流式回退成功必须补发 content_delta，否则 UI 零帧=空回复表象"
        )
        assert ctx.bus.frames("thinking_delta") == ["思考链"], "reasoning 必须补发为 thinking_delta"

    @pytest.mark.asyncio
    async def test_frame_shape_matches_stream_thinking(self) -> None:
        """下游 recorder/WS/前端按固定字段解析——补发帧必须与流式同形。"""
        ctx = _Ctx(stream_to_ui=True)
        await _emit_aggregated_response_as_deltas(ctx, _resp())
        assert ctx.bus.events, "应至少发出一帧"
        for event in ctx.bus.events:
            assert event.type is EventType.LLM_STREAM_EVENT
            assert isinstance(event.data, dict)
            assert event.data.get("type") in {"thinking_delta", "content_delta"}
            assert "content" in event.data

    @pytest.mark.asyncio
    async def test_empty_content_emits_nothing(self) -> None:
        """真空回复不得凭空造帧（否则制造「有内容」的假象）。"""
        ctx = _Ctx(stream_to_ui=True)
        await _emit_aggregated_response_as_deltas(ctx, _resp(content="", reasoning=""))
        assert ctx.bus.events == [], "content 与 reasoning 均空时不得补发任何帧"

    @pytest.mark.asyncio
    async def test_disabled_stream_emits_nothing(self) -> None:
        ctx = _Ctx(stream_to_ui=False)
        await _emit_aggregated_response_as_deltas(ctx, _resp())
        assert ctx.bus.events == [], "stream_to_ui=False 时必须零帧（不得无谓推流）"


class TestFilterFailuresCannotResurrectEmptyReply:
    @pytest.mark.asyncio
    async def test_filter_exception_does_not_block_deltas(self) -> None:
        """破坏者视角：过滤器抛错若冒泡，补发被跳过 → 空回复原样复现。"""
        from bandi.core.exceptions import BandiError

        class _Boom:
            def sanitize(self, text: str) -> str:
                raise BandiError("filter exploded")

        ctx = _Ctx(stream_to_ui=True, pii=_Boom())
        await _emit_aggregated_response_as_deltas(ctx, _resp())
        assert ctx.bus.frames("content_delta") == ["答案 2"], (
            "过滤器异常不得阻断补发（否则空回复第二形态复现）"
        )

    @pytest.mark.asyncio
    async def test_sanitizer_applied_to_emitted_frames(self) -> None:
        class _Mask:
            def sanitize(self, text: str) -> str:
                return text.replace("2", "**")

        ctx = _Ctx(stream_to_ui=True, sanitizer=_Mask())
        await _emit_aggregated_response_as_deltas(ctx, _resp())
        assert ctx.bus.frames("content_delta") == ["答案 **"], "补发帧必须过输出脱敏"
