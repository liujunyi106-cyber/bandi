# 功能：契约——reasoning-only 重试不得重复补发流帧，答案不得丢失。
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.states import _guard_reasoning_only, _is_reasoning_only
from bandi.core.message import LLMResponse


def _resp(content: str, reasoning: str = "") -> LLMResponse:
    return LLMResponse(content=content, reasoning=reasoning, tool_calls=[], finish_reason="stop")


class _Ctx:
    """功能：守卫所需的最小上下文（记录发出的流帧）。"""

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {}
        self.frames: list[tuple[str, str]] = []
        self.provider_name = "stub"
        self.turn_index = 1
        self.provider: Any = None
        self.messages: list[Any] = []
        self.active_tools: list[Any] = []
        self.session_id = "probe"

    class _Cfg:
        stream_to_ui = True
        llm = None

    config = _Cfg()

    class _Bus:
        async def emit(self, event: Any) -> None:
            return None

    bus = _Bus()

    def emit_delta(self, kind: str, text: str) -> None:
        self.frames.append((kind, text))


class _ScriptedProvider:
    """功能：按序返回预置响应，用于复现 reasoning-only → 重试 的形态。"""

    supports_streaming = False
    model_name = "scripted-probe"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def complete(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class _RecordingBus:
    """功能：记录事件，用于断言自愈结果口径。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class TestReasoningOnlyDetection:
    """检测口径：正文空 + 推理非空 = reasoning-only。"""

    def test_reasoning_only_detected(self) -> None:
        assert _is_reasoning_only(_resp("", "思考中"), safety_finish=False) is True

    def test_plain_empty_not_reasoning_only(self) -> None:
        assert _is_reasoning_only(_resp(""), safety_finish=False) is False

    def test_normal_content_not_reasoning_only(self) -> None:
        assert _is_reasoning_only(_resp("答案", "思考"), safety_finish=False) is False

    def test_safety_finish_excluded(self) -> None:
        """内容过滤/拒答属安全语义，不得当作 reasoning-only 去重试。"""
        assert _is_reasoning_only(_resp("", "思考"), safety_finish=True) is False


class TestNoDuplicateStreamFrames:
    """核心契约：重试成功后只补发一次，且补发的是最终答案。"""

    @pytest.mark.asyncio
    async def test_retry_result_is_the_one_emitted(self, monkeypatch: Any) -> None:
        ctx = _Ctx()
        first = _resp("", "推理")

        async def fake_retry(*_a: Any, **_kw: Any) -> LLMResponse:
            return _resp("最终答案")

        monkeypatch.setattr("bandi.agent.states._retry_reasoning_only", fake_retry)
        out = await _guard_reasoning_only(ctx, first, [], True)
        assert out.content == "最终答案", "守卫必须返回重试后的答案"

    @pytest.mark.asyncio
    async def test_failed_retry_returns_original(self, monkeypatch: Any) -> None:
        ctx = _Ctx()
        first = _resp("", "推理")

        async def fake_retry(*_a: Any, **_kw: Any) -> None:
            return None

        monkeypatch.setattr("bandi.agent.states._retry_reasoning_only", fake_retry)
        out = await _guard_reasoning_only(ctx, first, [], True)
        assert out is first, "重试失败必须原样返回，不得吞掉响应"

    @pytest.mark.asyncio
    async def test_single_retry_per_turn(self, monkeypatch: Any) -> None:
        """只试一次：同一轮第二次 reasoning-only 不再重试。

        闸门状态由 `_retry_reasoning_only` 自身维护（置位后本轮不再重试），
        这里用真实实现验证置位语义，不替换它。
        """
        ctx = _Ctx()
        # 已在本轮重试过 → 守卫不得再次重试
        ctx.meta["reasoning_only_retried"] = True

        # 直接用真实重试函数：闸门已置位，它必须立刻返回 None 而不发起调用
        from bandi.agent.states import _retry_reasoning_only

        out = await _guard_reasoning_only(ctx, _resp("", "r2"), [], True)
        assert out.content == "", "闸门已置位时守卫必须原样返回，不得换模型重问"
        assert await _retry_reasoning_only(ctx, [], True) is None, (
            "闸门置位后重试函数必须直接返回 None"
        )


class TestEmitOrdering:
    """补发时机：必须先定稿再补发，不能先发空帧再重试。"""

    @pytest.mark.asyncio
    async def test_guard_runs_before_emit_in_fallback_path(self) -> None:
        """源码级不变式：complete_with_fallback 路径里守卫先于补发。"""
        import inspect

        from bandi.agent import states as mod

        src = inspect.getsource(mod._do_llm_call_inner)
        guard_at = src.find("_guard_reasoning_only(")
        # 找到 fallback 分支里的补发点（第一个 emit 出现在 guard 之后才算正确）
        emit_candidates = [
            i for i in range(len(src)) if src.startswith("_emit_aggregated_response_as_deltas(", i)
        ]
        assert guard_at > 0, "守卫必须存在"
        assert emit_candidates, "补发点必须存在"
        first_emit = emit_candidates[0]
        assert first_emit > guard_at, (
            "补发必须发生在 reasoning-only 守卫之后——否则先发空帧、再重试，用户会看到重复或闪烁"
        )


class TestExactlyOneEmit:
    """补发归属：守卫内重试路径自己补发换回的答案，外层只补发原响应。"""

    @pytest.mark.asyncio
    async def test_recovered_retry_emits_exactly_once(self, monkeypatch: Any) -> None:
        """重试挽回时必须只补发 1 次——内外各补一次会让用户看到两帧相同内容。"""
        from bandi.agent import states as mod

        provider = _ScriptedProvider([_resp("", "只写思维链"), _resp("答案是 42。")])
        frames: list[str] = []
        ctx = _Ctx()

        original = mod._emit_aggregated_response_as_deltas

        async def spy(c: Any, response: Any) -> None:
            frames.append(getattr(response, "content", "") or "")
            await original(c, response)

        monkeypatch.setattr(mod, "_emit_aggregated_response_as_deltas", spy)
        ctx.provider = provider

        out = await mod._do_llm_call_inner(
            ctx=ctx,
            provider_messages=[{"role": "user", "content": "1+1"}],
            use_fallback=False,
            registry=None,
            bandi_config=None,
            allow_stream=True,
        )

        assert out.content == "答案是 42。", "必须返回重试后的答案"
        assert provider.calls == 2, f"应调用 2 次（首次+重试），实际 {provider.calls}"
        assert len(frames) == 1, f"应只补发 1 次，实际 {len(frames)} 次: {frames}"
        assert "答案是 42" in frames[0], "补发的必须是重试后的正文，不是被替换的空帧"

    @pytest.mark.asyncio
    async def test_normal_response_emits_once(self, monkeypatch: Any) -> None:
        """正常响应不得补发两次，也不得触发重试。"""
        from bandi.agent import states as mod

        provider = _ScriptedProvider([_resp("42", "thinking")])
        frames: list[str] = []
        ctx = _Ctx()

        original = mod._emit_aggregated_response_as_deltas

        async def spy(c: Any, response: Any) -> None:
            frames.append(getattr(response, "content", "") or "")
            await original(c, response)

        monkeypatch.setattr(mod, "_emit_aggregated_response_as_deltas", spy)
        ctx.provider = provider

        out = await mod._do_llm_call_inner(
            ctx=ctx,
            provider_messages=[{"role": "user", "content": "1+1"}],
            use_fallback=False,
            registry=None,
            bandi_config=None,
            allow_stream=True,
        )

        assert out.content == "42"
        assert provider.calls == 1, f"正常响应不得重试，实际调用 {provider.calls} 次"
        assert len(frames) == 1, f"应只补发 1 次，实际 {len(frames)} 次"


class TestSameShapeOutcome:
    """自愈结果口径：重试仍只返思维链必须记 same_shape，不是 failed。"""

    @pytest.mark.asyncio
    async def test_same_shape_outcome_recorded(self, monkeypatch: Any) -> None:
        from bandi.agent import states as mod

        provider = _ScriptedProvider(
            [_resp("", "第一次只写思维链"), _resp("", "第二次也只写思维链")]
        )
        ctx = _Ctx()
        ctx.provider = provider
        bus = _RecordingBus()
        ctx.bus = bus

        out = await mod._do_llm_call_inner(
            ctx=ctx,
            provider_messages=[{"role": "user", "content": "1+1"}],
            use_fallback=False,
            registry=None,
            bandi_config=None,
            allow_stream=False,
        )

        outcomes = [
            e.data.get("outcome")
            for e in bus.events
            if getattr(e, "data", {}).get("trigger") == "reasoning_only_retry"
        ]
        assert outcomes == ["same_shape"], (
            f"两次都只返思维链必须记 same_shape（实际 {outcomes}）——"
            "ProviderChainExhaustedError 的 args[0] 是消息字符串，拿它做 isinstance 恒假"
        )
        assert provider.calls == 2, "只试一次，不烧钱"
        assert out.reasoning, "reasoning 必须保留，不得清空"
