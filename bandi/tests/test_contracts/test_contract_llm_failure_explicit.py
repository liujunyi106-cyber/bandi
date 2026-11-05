# 功能：契约——LLM 失败显式化（T-smoke-1 ）：429/refusal/全候选耗尽必须置
# MODEL_ERROR 显式失败，杜绝「空 reply + VERIFIED 假成功」；瞬时连接错误保留重试
"""
recovery_manager._classify：HTTP status >= 400 的异常 → TERMINATE(MODEL_ERROR)
   （429/401/403 等确定性服务端拒绝，重试同 provider 无意义）
on_thinking 空响应兜底：call_model 返回 None 或空 content 无 tool_calls
   → MODEL_ERROR 显式终止而非静默继续
空响应兜底文案必须携带上游失败史（last_llm_error 链路），不得退化为 unknown
双链耗尽：失败史入 meta + 直调兜底仅一次 + 直调也败异常显式传播（不吞没）
项目自有 LLMError 保持 RECOVERABLE→RETRY（瞬时全败给恢复机会，ESCALATE 兜底）
回归保护：瞬时 ConnectionError 仍 RETRY；正常空 content+tool_calls 不误判。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from bandi.agent.base import RecoveryAction, TerminalReason
from bandi.agent.recovery_manager import RecoveryManager
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse
from bandi.llm.streaming import StreamEvent, StreamEventType


class TestP1HttpDeterministicFailure:
    def test_429_classified_recoverable(self) -> None:
        # 语义回调（用户拍板，对齐参考源）：429 限流属**暂时故障** → 可重试（带退避），
        # 只有 4xx 非 429（配置/请求类）才是确定性失败。
        rm = RecoveryManager()
        err = httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "https://x"), response=httpx.Response(429)
        )
        severity = rm._classify(err, "thinking")
        assert (
            severity
            != __import__(
                "bandi.agent.recovery_manager", fromlist=["ErrorSeverity"]
            ).ErrorSeverity.FATAL
        )

    def test_handle_not_terminate_for_429(self) -> None:
        import asyncio

        rm = RecoveryManager()
        err = httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "https://x"), response=httpx.Response(429)
        )
        action = asyncio.run(rm.handle(err, "thinking"))
        assert action != RecoveryAction.TERMINATE

    @pytest.mark.parametrize("status", [401, 403, 404, 413])
    def test_handle_terminate_for_config_class_status(self, status: int) -> None:
        import asyncio

        rm = RecoveryManager()
        err = httpx.HTTPStatusError(
            str(status), request=httpx.Request("POST", "https://x"), response=httpx.Response(status)
        )
        action = asyncio.run(rm.handle(err, "thinking"))
        assert action == RecoveryAction.TERMINATE, f"{status}（配置/请求类）应确定性终止"

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_handle_not_terminate_for_server_status(self, status: int) -> None:
        import asyncio

        rm = RecoveryManager()
        err = httpx.HTTPStatusError(
            str(status), request=httpx.Request("POST", "https://x"), response=httpx.Response(status)
        )
        action = asyncio.run(rm.handle(err, "thinking"))
        assert action != RecoveryAction.TERMINATE, f"{status}（上游暂时故障）应可重试"


class TestP2EmptyResponseGuard:
    @pytest.mark.asyncio
    async def test_empty_response_sets_model_error(self) -> None:
        """call_model 返回空响应（无 content 无 tool_calls）时 on_thinking 必须
        置 MODEL_ERROR 显式失败——这是限流期「假 VERIFIED」的机制根源（T-smoke-1）。

        自包含构造：QueryDeps.call_model 打桩返回空 LLMResponse
        on_thinking 消费后断言 terminal_reason。"""
        from types import SimpleNamespace

        from bandi.agent.base import ExecutionContext
        from bandi.agent.deps import QueryDeps
        from bandi.agent.states import on_thinking
        from bandi.core.config import AgentConfig
        from bandi.core.message import LLMResponse, Message, MessageRole

        empty = LLMResponse(content="", tool_calls=None, finish_reason="stop")

        async def call_model(ctx: Any, messages: list[dict]) -> LLMResponse:
            return empty

        async def noop_compact(ctx: Any) -> None:
            raise AssertionError("空响应不应触发压缩链")

        deps = QueryDeps(
            call_model=call_model,
            autocompact=noop_compact,
            microcompact=noop_compact,
            new_id=lambda: "x",
        )
        provider = SimpleNamespace(model_name="stub", protocol="openai", supports_streaming=False)
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
            config=AgentConfig(),
            bus=EventBus(),
        )
        ctx.query_deps = deps
        ctx.raw_input = "hi"

        await on_thinking(ctx)

        assert ctx.terminal_reason == TerminalReason.MODEL_ERROR, (
            f"空响应被静默消费：reason={ctx.terminal_reason} content={ctx.current_content!r}"
        )


class TestP3TransientStillRetries:
    def test_connection_error_still_retry(self) -> None:
        """回归保护：瞬时连接错误保持 RECOVERABLE→RETRY（不误杀合理退避）。"""
        import asyncio

        rm = RecoveryManager()
        err = ConnectionError("connection reset")
        action = asyncio.run(rm.handle(err, "thinking"))
        assert action == RecoveryAction.RETRY


def _async_return(value: Any):
    async def _fn(*a: Any, **k: Any) -> Any:
        return value

    return _fn


async def _async_none(ctx: Any):
    from bandi.agent.deps import CompactOutcome

    return CompactOutcome(success=False, messages=[], stats=None)


# ── 上游失败史必须进入空响应错误文案（last_llm_error 链路） ──────────


class _FakeRegistryHolder:
    def all(self) -> list[Any]:
        return [object()]


class _FakeFallback:
    """候选池桩：ordered_candidates 空列表跳过流式循环；complete_with_fallback 必败。"""

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def ordered_candidates(self, preferred: str, messages: list) -> list[str]:
        return []

    async def complete_with_fallback(self, **kwargs: Any):
        raise RuntimeError("upstream quota dead")


def _fallback_ctx(monkeypatch: pytest.MonkeyPatch, provider: Any, chain: list[str]) -> Any:
    """构造走 use_fallback=True 生产路径的 ctx（registry/config 注入 meta）。"""
    from types import SimpleNamespace

    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig, BandiConfig, LLMConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole

    monkeypatch.setattr(
        "bandi.llm.capabilities.registry.get_capability_registry",
        lambda: _FakeRegistryHolder(),
    )
    monkeypatch.setattr("bandi.llm.strategy.CapabilityFallback", _FakeFallback)

    llm = LLMConfig(fallback_chain=chain)
    bandi_config = BandiConfig(llm=llm)
    bandi_config.agent = AgentConfig()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.meta["provider_registry"] = SimpleNamespace()
    ctx.meta["bandi_config"] = bandi_config
    ctx.raw_input = "hi"
    return ctx


class _EmptyCompleteProvider:
    """complete 恒返回空响应的 provider 桩（带调用计数与 close）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "empty-stub"

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def supports_streaming(self) -> bool:
        return False

    async def complete(self, messages: list, tools: Any = None, **kwargs: Any):
        self.calls += 1
        return LLMResponse(content="", tool_calls=None, finish_reason="stop")

    async def stream(self, messages: list, tools: Any = None, **kwargs: Any):
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def close(self) -> None:
        pass


class _EmptyFallbackEngine:
    """配置链「成功」返回空响应——模拟 Capability 全败后 FallbackEngine 成功但内容空的场景。"""

    def __init__(self, chain: Any) -> None:
        pass

    async def call_with_fallback(self, messages: list, first: Any, caller: Any):
        return LLMResponse(content="", tool_calls=None, finish_reason="stop")


class TestP4UpstreamErrorCarriedToFinalMessage:
    @pytest.mark.asyncio
    async def test_capability_exhausted_empty_response_carries_root_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候选池失败 → 直接显式抛 LLMError 且携带上游根因（候选链已删——
        不搞候选，配置什么模型用什么）。诊断链不可断：消息必须含
        'upstream quota dead' 而非 'unknown'；且不得直调已失效 provider。"""
        from bandi.agent.states import on_thinking
        from bandi.core.exceptions import LLMError

        provider = _EmptyCompleteProvider()
        ctx = _fallback_ctx(monkeypatch, provider, chain=[])

        with pytest.raises(LLMError, match="upstream quota dead"):
            await on_thinking(ctx)
        assert "upstream quota dead" in ctx.meta.get("last_llm_error", ""), (
            f"last_llm_error 未写入根因：{ctx.meta.get('last_llm_error')!r}"
        )
        assert provider.calls == 0, "候选池失败后直调了失效 provider——静默伪装复活"


# ── 双链耗尽后的直调兜底语义（诊断链不断 + 失败显式传播） ────────────


class _DeadFallbackEngine:
    def __init__(self, chain: Any) -> None:
        pass

    async def call_with_fallback(self, messages: list, first: Any, caller: Any):
        raise RuntimeError("chain also dead")


class _DeadCompleteProvider(_EmptyCompleteProvider):
    async def complete(self, messages: list, tools: Any = None, **kwargs: Any):
        self.calls += 1
        raise RuntimeError("direct call dead too")


class TestP5BothChainsExhaustedExplicitOutcome:
    @pytest.mark.asyncio
    async def test_chain_failure_recorded_and_raises_llm_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """（破坏者视角）：候选池失败后必须显式抛 LLMError（含根因）——若实现
        fall-through 再调一次 provider.complete（同已失效 provider），本用例红。
        失败史必须进 last_llm_error 供诊断。"""
        import pytest as _pytest

        from bandi.agent.states import on_thinking
        from bandi.core.exceptions import LLMError

        provider = _DeadCompleteProvider()
        ctx = _fallback_ctx(monkeypatch, provider, chain=[])

        with _pytest.raises(LLMError, match="upstream quota dead"):
            await on_thinking(ctx)
        assert provider.calls == 0, (
            f"候选池耗尽后仍直调了失效 provider {provider.calls} 次——静默伪装复活"
        )
        assert "upstream quota dead" in ctx.meta.get("last_llm_error", "")


# ── 项目自有 LLMError 的分类行为（绿3 raise 后的恢复链语义） ──────────


class TestP6LLMErrorClassification:
    def test_llm_error_keeps_retry_not_terminate(self) -> None:
        """绿3 抛出的 LLMError（无 HTTP status）保持 RECOVERABLE→RETRY——
        全候选瞬时失败给退避恢复机会，连续 10 次由 ESCALATE 兜底；
        若误分类 FATAL 会把可恢复的限流窗口变成会话猝死。"""
        import asyncio

        from bandi.core.exceptions import LLMError

        rm = RecoveryManager()
        action = asyncio.run(rm.handle(LLMError("all candidates failed"), "thinking"))
        assert action == RecoveryAction.RETRY


# ── run_agent_turn 生产全链端到端——空响应不得产出假成功 reply ───────


class _E2EEmptyFallbackEngine:
    """端到端桩：配置链「成功」返回空响应（避免测试出网），全链产出空 LLMResponse。"""

    def __init__(self, chain: Any) -> None:
        pass

    async def call_with_fallback(self, messages: list, first: Any, caller: Any):
        return LLMResponse(content="", tool_calls=None, finish_reason="stop")


class TestP7E2EEmptyResponseExplicit:
    @pytest.mark.asyncio
    async def test_run_agent_turn_empty_provider_yields_model_error_reply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """端到端契约（生产主链路 run_agent_turn）——call_model 全链产出空
        响应时，最终 AgentTurnResult 必须 terminal_reason=MODEL_ERROR 且 reply
        含显式错误语义；reply='' 假成功即红（agent_runner 提取点被钉住）。"""
        from pathlib import Path

        from bandi.core.agent_runner import run_agent_turn
        from bandi.memory.session.store import SessionStore

        monkeypatch.setattr(
            "bandi.core.agent_runner.build_provider",
            lambda cfg: _EmptyCompleteProvider(),
        )
        # 配置链打桩返回空响应：候选池对 unknown model 必败后由本桩接管
        # 既避免真实出网，又保证「成功路径的空响应」穿透整条生产装配链
        monkeypatch.setattr("bandi.llm.strategy.FallbackEngine", _E2EEmptyFallbackEngine)
        # 本用例覆盖「配置链」路径（默认链已按 2026-10-09 拍板置空，机制仍在）
        from bandi.core.config import BandiConfig

        _real_load = BandiConfig.load.__func__

        def _load_with_chain(cls: type) -> Any:
            cfg = _real_load(cls)
            cfg.llm.fallback_chain = ["stub-chain-model"]
            return cfg

        monkeypatch.setattr(BandiConfig, "load", classmethod(_load_with_chain))
        store = SessionStore(str(Path(tmp_path) / "s.db"))
        r = await run_agent_turn(
            user_input="你好",
            session_store=store,
            project_root=str(Path(tmp_path) / "proj"),
        )
        reason = getattr(r.terminal_reason, "name", r.terminal_reason)
        assert reason == "MODEL_ERROR", f"端到端假成功：reason={reason} reply={r.reply[:80]!r}"
        # 无候选链：失败经显式错误语义透出（非空假成功）
        assert "LLM call failed" in (r.reply or ""), (
            f"reply 缺显式错误语义（提取点丢弃 final_message？）：{r.reply[:120]!r}"
        )


# ── 流式中途 raise 异常必须传播，不得吞成空响应假成功 ────────────────


class TestP8StreamMidwayRaisePropagates:
    @pytest.mark.asyncio
    async def test_stream_raise_midway_not_swallowed(self) -> None:
        """（破坏者视角）：流已产出部分 delta 后 stream() 直接 raise——异常必须
        传播出 call_model 链（由 loop/recovery 显式处置）；任何「catch 后返回空
        LLMResponse」的实现都会让部分输出伪装成完整回答，本用例红。"""
        from collections.abc import AsyncIterator

        import pytest as _pytest

        from bandi.agent.base import ExecutionContext
        from bandi.agent.states import on_thinking
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.core.message import Message, MessageRole
        from bandi.llm.streaming import StreamEvent, StreamEventType

        class _MidwayRaiseStreamProvider:
            @property
            def model_name(self) -> str:
                return "midway-stub"

            @property
            def protocol(self) -> str:
                return "openai"

            @property
            def supports_streaming(self) -> bool:
                return True

            async def complete(self, messages: list, tools: Any = None, **k: Any):
                raise AssertionError("非流式路径不应被触达")

            async def stream(
                self, messages: list, tools: Any = None, **k: Any
            ) -> AsyncIterator[StreamEvent]:
                yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="partial an")
                raise RuntimeError("midway boom")

        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=_MidwayRaiseStreamProvider(),
            config=AgentConfig(),
            bus=EventBus(),
        )
        ctx.query_deps = None
        ctx.raw_input = "hi"

        with _pytest.raises(RuntimeError, match="midway boom"):
            await on_thinking(ctx)
