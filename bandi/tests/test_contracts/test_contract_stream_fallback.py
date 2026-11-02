# 功能：流式遮蔽修复契约（技术债 #16——use_fallback 生产恒真遮蔽 _stream_thinking）
"""
  生产形态流式可达：provider_registry 注入（use_fallback=True）+
     stream_to_ui=True + provider 支持流式 → THINKING_DELTA/CONTENT_DELTA
     经 bus 可达（技术债 #16 主断言——修复前该路径被非流式 fallback 遮蔽）
  流式候选失败降级非流式：候选 stream 抛错 → complete_with_fallback 兜底成功
  stream_to_ui=False 行为不变：流式零调用，非流式直返
  超长穿透：流式中 prompt too long 错误上抛响应式压缩层，不得被降级链吞掉
  流式形态工具批边界 steer drain（p-fix 遗留补）：on_executing drain +
     on_thinking 流式请求面含 steer 消息

破坏者断言：超长错误不得被降级链吞掉；无 registry 时旧直调路径保持。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall

# ── 测试基础设施 ──────────────────────────────────────────────────────────────


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def stream_datas(self) -> list[dict]:
        return [e.data for e in self.events if e.type == EventType.LLM_STREAM_EVENT]


class _StreamStubProvider:
    """流式 stub provider——可配置 stream 抛错/事件序列/非流式响应。"""

    def __init__(
        self,
        name: str = "stub-a",
        stream_error: Exception | None = None,
        events: list[Any] | None = None,
        response: LLMResponse | None = None,
        supports_streaming_flag: bool = True,
    ) -> None:
        self._name = name
        self._stream_error = stream_error
        self._events = events or []
        self._response = response or LLMResponse(content="非流式回退")
        self._supports = supports_streaming_flag
        self.stream_calls = 0
        self.complete_calls = 0
        self.stream_messages: list[list[dict]] = []

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def supports_streaming(self) -> bool:
        return self._supports

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def config(self) -> None:
        return None

    async def stream(self, messages: list[dict], tools: Any = None, **kw: Any) -> Any:
        self.stream_calls += 1
        self.stream_messages.append(messages)
        if self._stream_error is not None:
            raise self._stream_error
        for e in self._events:
            yield e

    async def complete(self, messages: list[dict], tools: Any = None, **kw: Any) -> LLMResponse:
        self.complete_calls += 1
        return self._response


class _RegistryStub:
    def __init__(self, providers: dict[str, Any]) -> None:
        self._providers = providers

    def get_provider(self, model: str, config: Any) -> Any:
        if model not in self._providers:
            raise KeyError(model)
        return self._providers[model]


def _stream_events() -> list[Any]:
    from bandi.llm.streaming import StreamEvent, StreamEventType

    return [
        StreamEvent(type=StreamEventType.THINKING_DELTA, thinking_delta="推理中"),
        StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="最终答案"),
        StreamEvent(
            type=StreamEventType.DONE,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        ),
    ]


def _make_ctx(provider: Any, registry: Any, cfg: BandiConfig, bus: _Bus) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        terminal_reason=None,
        final_message=None,
        turn_index=1,
        provider=provider,
        provider_name=provider.model_name,
        config=cfg.agent,
        consumed_tokens=0,
        total_tokens=100,
        bus=bus,
        meta={
            "provider_registry": registry,
            "bandi_config": cfg,
            "tracer": None,
            "task_ledger": None,
            "session_id": "sess-stream",
        },
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="问"),
        ],
        cot_trace=[],
        active_tools=[],
        query_deps=None,
        raw_input="问",
        clarification_count=0,
        clarification_injected=False,
        current_response=None,
        current_content=None,
        current_tool_calls=None,
        tool_results=[],
        needs_compression=lambda: False,
    )


async def _call_llm(ctx: Any) -> LLMResponse:
    """经生产入口 _do_llm_call 调用（use_fallback=True 生产形态）。"""
    from bandi.agent import states as states_mod

    return await states_mod._do_llm_call(
        ctx,
        [m for m in ctx.messages],
        use_fallback=True,
        registry=ctx.meta["provider_registry"],
        bandi_config=ctx.meta["bandi_config"],
    )


# ── 生产形态流式可达 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_production_fallback_path_streams_deltas() -> None:
    """契约：registry 注入生产形态 + stream_to_ui → THINKING_DELTA/
    CONTENT_DELTA 经 bus 可达，响应由流式组装（技术债 #16 主断言）。"""
    prov = _StreamStubProvider(events=_stream_events())
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())

    resp = await _call_llm(ctx)

    assert resp.reasoning == "推理中", f"思考增量必须进 reasoning（实际 {resp.reasoning!r}）"
    assert resp.content == "最终答案"
    datas = ctx.bus.stream_datas()
    types = [d.get("type") for d in datas]
    assert "thinking_delta" in types, f"THINKING_DELTA 必须经 bus 可达（实际 {types}）"
    assert "content_delta" in types, f"CONTENT_DELTA 必须经 bus 可达（实际 {types}）"
    assert prov.stream_calls == 1 and prov.complete_calls == 0, "必须走流式而非非流式"


@pytest.mark.asyncio
async def test_streaming_success_records_provider_stats() -> None:
    """契约（B-q2+破坏者）：流式成功必须入 provider_stats——CapabilityRouter._rank
    按 reliability 排序，成功零记录会把健康 provider 稀释到队尾（与非流式
    complete_with_fallback 成功记账不对称）。"""
    from bandi.llm.capabilities.provider_stats import (
        get_provider_stats,
        reset_provider_stats,
    )

    reset_provider_stats()
    try:
        prov = _StreamStubProvider(events=_stream_events())
        ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())

        await _call_llm(ctx)

        stat = get_provider_stats().get("chat", "stub-a")
        assert stat is not None and stat.successes >= 1, (
            f"流式成功必须 record_attempt(success=True)（实际 {stat}）"
        )
    finally:
        reset_provider_stats()


# ── 流式失败降级非流式 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_failure_degrades_to_non_streaming() -> None:
    """契约：候选池首选流式抛连接错 → 降级非流式 complete_with_fallback
    成功兜底，turn 不中断。"""
    prov = _StreamStubProvider(stream_error=RuntimeError("connection reset"))
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())

    resp = await _call_llm(ctx)

    assert resp.content == "非流式回退", "流式失败必须降级到非流式兜底"
    assert prov.stream_calls == 1 and prov.complete_calls >= 1


@pytest.mark.asyncio
async def test_provider_without_streaming_skips_stream_branch() -> None:
    """契约（P3a）：provider 不支持流式时跳过流式分支走非流式。"""
    prov = _StreamStubProvider(supports_streaming_flag=False)
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())

    resp = await _call_llm(ctx)

    assert resp.content == "非流式回退"
    assert prov.stream_calls == 0 and prov.complete_calls >= 1


@pytest.mark.asyncio
async def test_stream_to_ui_false_keeps_non_streaming() -> None:
    """契约（P3b）：stream_to_ui=False 行为与修复前一致——流式零调用。"""
    prov = _StreamStubProvider()
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())
    ctx.config = SimpleNamespace(**{**ctx.config.__dict__, "stream_to_ui": False})

    resp = await _call_llm(ctx)

    assert resp.content == "非流式回退"
    assert prov.stream_calls == 0, "stream_to_ui=False 不得尝试流式"


# ── 超长穿透 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_too_long_in_stream_propagates() -> None:
    """契约（+破坏者）：流式中超长错误必须穿透降级链上抛——候选池每个模型"""
    from bandi.agent import states as states_mod

    too_long = RuntimeError(
        "This model's maximum context length is 4096 tokens. "
        "However, your messages resulted in 5000 tokens."
    )
    prov = _StreamStubProvider(stream_error=too_long)
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), BandiConfig(), _Bus())

    with pytest.raises(RuntimeError, match="maximum context"):
        await states_mod._do_llm_call_inner(
            ctx, list(ctx.messages), True, ctx.meta["provider_registry"], ctx.meta["bandi_config"]
        )


# ── 工具批边界 drain × 流式请求面（p-fix 遗留补）───────────────────────


@pytest.mark.asyncio
async def test_tool_batch_boundary_drain_reaches_stream_request() -> None:
    """契约：工具批执行后 on_executing drain steer 注入 USER，紧接的
    on_thinking 流式请求面包含 steer 文本且队列清空（p-fix 回归钉）。"""

    from bandi.agent import states as states_mod
    from bandi.agent.steer import get_default_registry
    from bandi.core.message import ToolResult

    sid = "sess-stream-drain"
    get_default_registry().push(sid, "转向：先看测试文件")

    executed: list[list] = []

    class _Executor:
        async def execute(self, calls: list) -> list:
            executed.append(calls)
            return [
                ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    output="ok",
                    success=True,
                    duration_ms=1,
                )
                for tc in calls
            ]

    prov = _StreamStubProvider(events=_stream_events())
    cfg = BandiConfig()
    bus = _Bus()
    ctx = _make_ctx(prov, _RegistryStub({"stub-a": prov}), cfg, bus)
    ctx.session_id = sid
    ctx.tool_executor = _Executor()
    ctx.loop_detection_window = 5
    ctx.tool_sequence_hashes = []
    ctx.current_tool_calls = [ToolCall(id="c1", name="read", arguments={"x": 1})]
    ctx.tool_results = None

    await states_mod.on_executing(ctx)

    drained = get_default_registry().drain(sid)
    assert drained == [], f"工具批边界必须 drain steer 队列（实际 {drained}）"

    # 第二轮 thinking：流式请求面必须含 steer 注入的 USER 消息
    ctx.current_tool_calls = None
    ctx.turn_index = 2
    await states_mod.on_thinking(ctx)

    assert prov.stream_calls == 1, "修复后生产形态必须走流式"
    request_text = str(prov.stream_messages[0])
    assert "转向：先看测试文件" in request_text, (
        f"steer 注入必须进入流式 LLM 请求面（实际 {request_text[:300]}）"
    )
