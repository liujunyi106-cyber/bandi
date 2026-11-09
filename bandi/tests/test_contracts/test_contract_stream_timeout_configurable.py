# 功能：契约——单次流式调用整体超时可配（`llm.stream_timeout`）：
# 高思考档模型单次生成可达数分钟，240s 硬常量会掐断长推理（2026-10-19 实锤：
# opencode-go DeepSeek V4.1 Flash 思考=max 时 planner 调用流式超时 240s → 任务失败）。
# 缺省 240s 防挂起不变；档案/config 可放宽（open-code-go 档案置 900s）。
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.core.config import AgentConfig, BandiConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse


class _StreamStub:
    model_name = "stub-stream"
    protocol = "openai"
    supports_streaming = True


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        messages=[],
        provider=_StreamStub(),
        config=AgentConfig(stream_to_ui=True),
        bus=EventBus(),
    )


def test_stream_timeout_reads_llm_config() -> None:
    from bandi.agent.states import _stream_total_timeout

    cfg = BandiConfig()
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"stream_timeout": 900.0})})
    assert _stream_total_timeout(cfg) == 900.0


def test_stream_timeout_default_is_240() -> None:
    from bandi.agent.states import _stream_total_timeout

    assert _stream_total_timeout(BandiConfig()) == 240.0, "缺省防挂起保护不得变化"


@pytest.mark.parametrize(
    "config",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(llm=None),
        SimpleNamespace(llm=SimpleNamespace(stream_timeout=None)),
        SimpleNamespace(llm=SimpleNamespace(stream_timeout=0)),
        SimpleNamespace(llm=SimpleNamespace(stream_timeout=-5)),
        SimpleNamespace(llm=SimpleNamespace(stream_timeout="abc")),
    ],
)
def test_stream_timeout_falls_back_on_garbage(config: object) -> None:
    """破坏者：空/零/负/非数值一律回落 240（禁止把垃圾当预算导致零超时）。"""
    from bandi.agent.states import _stream_total_timeout

    assert _stream_total_timeout(config) == 240.0


def test_stalling_stream_aborts_after_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为：配置 0.5s 预算 → 5s 未完成的流必须被中止并显式失败（不静默挂起）。"""
    from bandi.agent import states as states_mod

    async def _slow_stream(
        ctx: ExecutionContext, messages: list[dict], **kwargs: object
    ) -> LLMResponse:
        await asyncio.sleep(5)
        return LLMResponse(content="late")

    monkeypatch.setattr(states_mod, "_stream_thinking", _slow_stream)
    cfg = BandiConfig()
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"stream_timeout": 0.5})})
    ctx = _ctx()

    resp = asyncio.run(
        states_mod._do_llm_call_inner(
            ctx, [{"role": "user", "content": "hi"}], False, None, cfg, allow_stream=True
        )
    )

    assert resp.content == ""
    assert ctx.terminal_reason == TerminalReason.MODEL_ERROR
    assert "模型响应超时" in (ctx.final_message or "")


def test_relaxed_timeout_allows_long_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """复查（正例）：放宽后的预算真实生效——1s 的流在 5s 预算下正常返回。"""
    from bandi.agent import states as states_mod

    async def _slow_but_fine(
        ctx: ExecutionContext, messages: list[dict], **kwargs: object
    ) -> LLMResponse:
        await asyncio.sleep(1)
        return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(states_mod, "_stream_thinking", _slow_but_fine)
    cfg = BandiConfig()
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"stream_timeout": 5.0})})
    ctx = _ctx()

    resp = asyncio.run(
        states_mod._do_llm_call_inner(
            ctx, [{"role": "user", "content": "hi"}], False, None, cfg, allow_stream=True
        )
    )
    assert resp.content == "ok"
    assert ctx.terminal_reason != TerminalReason.MODEL_ERROR
