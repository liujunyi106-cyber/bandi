"""
响应式压缩处理——
  1. ``CapabilityFallback.complete_with_fallback``（主路径候选池）
  2. ``FallbackEngine.call_with_fallback``（配置声明链兜底）
  3. 直调 ``provider.complete``（无 fallback 场景）

错误驱动压缩与自动压缩共享同一 CompressionPipeline 出口：
"""

from types import SimpleNamespace

import httpx
import pytest

from bandi.agent.base import CompressionState, ExecutionContext, TerminalReason
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.llm.strategy import (
    CapabilityFallback,
    FallbackEngine,
    is_prompt_too_long_error,
)
from bandi.memory.compression_pipeline import ProgressiveCompressor

_LONG_ERROR_TEXTS = [
    "This model's maximum context length is 8192 tokens. However, your messages resulted in 12000 tokens.",
    "prompt is too long: 200000 tokens > 180000 maximum",
    "Request failed with status code 413: payload too large",
    "input length and `max_tokens` exceed context limit",
    "too many tokens in request",
]
_NOT_LONG_ERROR_TEXTS = [
    "invalid api key",
    "rate limit exceeded, retry after 5s",
    "connection reset by peer",
    "model not found",
]


class _RecordingBus:
    """功能：契约测试用事件总线，只记录不外发。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def _make_http_error(text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(400, text=text, request=request)
    return httpx.HTTPStatusError(f"Client error '400': {text}", request=request, response=response)


@pytest.mark.parametrize("text", _LONG_ERROR_TEXTS, ids=[t[:20] for t in _LONG_ERROR_TEXTS])
def test_is_prompt_too_long_matches_provider_variants(text: str) -> None:
    """契约：各 provider 超长错误文本变体必须命中（httpx 400 文本 / 413 / 措辞）。"""
    assert is_prompt_too_long_error(_make_http_error(text)) is True
    assert is_prompt_too_long_error(RuntimeError(text)) is True


@pytest.mark.parametrize("text", _NOT_LONG_ERROR_TEXTS, ids=[t[:20] for t in _NOT_LONG_ERROR_TEXTS])
def test_is_prompt_too_long_does_not_misfire_on_other_errors(text: str) -> None:
    """契约（破坏者视角：防误伤）：限流/鉴权/网络/模型不存在等普通错误不得误判为超长
    （误判会导致正常失败被压缩重试，掩盖真因）。"""
    assert is_prompt_too_long_error(_make_http_error(text)) is False
    assert is_prompt_too_long_error(RuntimeError(text)) is False


def test_classify_maps_prompt_too_long_to_compact_action() -> None:
    """
    （RECOVERY_STRATEGIES['LLM_PROMPT_TOO_LONG'] 不再不可达）。"""
    engine = FallbackEngine(["p1"])
    action = engine._classify(_make_http_error(_LONG_ERROR_TEXTS[0]))
    assert action.action == "compact"
    assert action.compact_before_retry is True


@pytest.mark.asyncio
async def test_call_with_fallback_reraises_without_switching_on_compact() -> None:
    """契约（D-d3）：call_with_fallback 收到 compact action 时必须立即上抛原异常
    不得继续消耗后续 provider（超长换 provider 无意义）。"""
    calls: list[str] = []

    async def call(messages: list[dict], cfg: dict) -> LLMResponse:
        calls.append(cfg["provider"])
        raise _make_http_error(_LONG_ERROR_TEXTS[0])

    engine = FallbackEngine(["p1", "p2", "p3"])
    with pytest.raises(httpx.HTTPStatusError):
        await engine.call_with_fallback([{"role": "user", "content": "hi"}], {}, call)

    assert calls == ["p1"], f"compact 分支必须在首个 provider 上抛（实际调用 {calls}）"


@pytest.mark.asyncio
async def test_capability_fallback_short_circuits_on_prompt_too_long() -> None:
    """契约（D-d2 主路径止损）：CapabilityFallback 候选池遇超长必须立即上抛
    后续候选模型零调用（不再白烧网络请求）。"""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_provider(self, model: str, config):
            self.calls.append(model)
            return _FailingProvider(self)

        def all(self) -> list:
            return []

    class _FailingProvider:
        def __init__(self, registry: "_FakeRegistry") -> None:
            self._registry = registry

        @property
        def model_name(self) -> str:
            return "fake"

        async def complete(self, messages, **kwargs):
            raise _make_http_error(_LONG_ERROR_TEXTS[0])

    class _Stats:
        def record_attempt(self, *a, **k) -> None:
            pass

    registry = _FakeRegistry()
    fallback = CapabilityFallback(registry, config=SimpleNamespace(llm=None), stats=_Stats())
    fallback._router = type("_R", (), {"candidates": staticmethod(lambda *a, **k: [])})()

    with pytest.raises(httpx.HTTPStatusError):
        await fallback.complete_with_fallback(
            "model-a", [{"role": "user", "content": "hi"}], registry, fallback._config
        )

    assert len(registry.calls) == 1, f"超长短路后后续候选不得被调用（实际调用 {registry.calls}）"


class _SummaryOnceProvider:
    """功能：前 N 次调用抛超长错误，之后返回结构化摘要响应（模拟压缩生效后成功）。"""

    def __init__(self, fail_times: int, summary_text: str) -> None:
        self._fail_times = fail_times
        self._summary_text = summary_text
        self.call_count = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise _make_http_error(_LONG_ERROR_TEXTS[0])
        return LLMResponse(
            content=self._summary_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    @property
    def protocol(self) -> str:
        return "openai"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


_STRUCTURED_SUMMARY = (
    "## 目标\n- 完成 reactive compaction 验证\n\n"
    "## 进度\n### Done\n- [x] 触发超长\n"
    "### In Progress\n- [ ] 压缩重试\n"
    "### Blocked\n- (none)\n\n"
    "## 关键决策\n- **复用 pipeline**: 与自动压缩同构\n\n"
    "## 下一步\n1. 断言产物\n\n"
    "## 关键上下文\n- 错误串 RuntimeError('reactive_e2e') 见 logs/rc.py\n"
)


def _big_messages(count: int) -> list[Message]:
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content="请修复 logs/reactive.py 的 ValueError('seed')"),
    ]
    for i in range(count):
        msgs.append(
            Message(
                role=MessageRole.USER,
                content=f"第{i}轮排查：logs/run_{i}.py 报 ValueError('compaction failed')，请分析并给出补丁方案说明。",
            )
        )
        msgs.append(
            Message(
                role=MessageRole.ASSISTANT, content=f"第{i}轮结论见 patch_{i}.diff，已登记待验证"
            )
        )
    return msgs


def _make_ctx(provider: object, messages: list[Message]) -> ExecutionContext:
    # 窗口 400 经「输出预留 20K 下限 + 窗口 50% 钳制」→ effective=200
    # （与改动前 context_window=200 的 effective 等效——护栏边界场景保持可测）
    config = AgentConfig(context_window=400, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=list(messages),
        provider=provider,
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.total_tokens = sum(provider.count_tokens(m.content or "") for m in messages)
    ctx.compress = CompressionState(
        pipeline=__import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionPipeline"]
        ).CompressionPipeline(
            progressive=ProgressiveCompressor(max_chunks=3, target_token_budget=10)
        )
    )
    return ctx


@pytest.mark.asyncio
async def test_reactive_compact_retries_and_succeeds_with_boundary() -> None:
    from bandi.agent.states import _reactive_compact_retry

    provider = _SummaryOnceProvider(fail_times=0, summary_text=_STRUCTURED_SUMMARY)
    ctx = _make_ctx(provider, _big_messages(10))
    original_count = len(ctx.messages)
    raw_input = str(ctx.messages[1].content)
    ctx.raw_input = raw_input

    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert result is not None
    assert "[上下文摘要]" in str(result.content) or result.content, (
        "重试成功的响应必须是真实 LLM 输出"
    )
    boundaries = [
        m
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith("[压缩边界]")
    ]
    assert len(boundaries) == 1, "错误驱动压缩必须插入恰一条 [压缩边界]（与自动压缩同构）"
    summaries = [
        m
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM and "[上下文摘要]" in (m.content or "")
    ]
    assert len(summaries) == 1, "压缩产物恰一条摘要"
    assert len(ctx.messages) < original_count, "消息数必须实际缩减"


class _CompactedStillTooLongProvider:
    """功能：重试请求若含压缩摘要（重建后的消息）则仍抛超长，否则返回摘要响应——
    覆盖「首次压缩重试仍超长 → 第二次压缩」分支。"""

    def __init__(self, summary_text: str) -> None:
        self._summary_text = summary_text
        self.call_contents: list[list[str]] = []

    async def complete(self, messages, tools=None, **kwargs):
        contents = [str(m.get("content", "")) for m in messages]
        self.call_contents.append(contents)
        if any("[上下文摘要]" in c for c in contents):
            raise _make_http_error(_LONG_ERROR_TEXTS[0])
        return LLMResponse(
            content=self._summary_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    @property
    def protocol(self) -> str:
        return "openai"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.mark.asyncio
async def test_reactive_compact_second_attempt_guarded_when_no_further_reduction() -> None:
    """契约（D-d1 框架 + 护栏）：首次压缩重试仍超长时进入第二次尝试；
    当上下文已不足再次分块压缩（低于最小门槛）时护栏必须终止并激活
    PROMPT_TOO_LONG，不得烧无效 LLM 重试。"""
    from bandi.agent.states import _reactive_compact_retry

    provider = _CompactedStillTooLongProvider(_STRUCTURED_SUMMARY)
    ctx = _make_ctx(provider, _big_messages(10))
    ctx.raw_input = str(ctx.messages[1].content)

    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert result is None, "无法进一步压缩时必须终止"
    assert ctx.terminal_reason == TerminalReason.PROMPT_TOO_LONG
    # 护栏：对话重试只发生在首次压缩后一次（第二次尝试未再产生任何 LLM 消耗）
    retry_calls = [c for c in provider.call_contents if not any("对话摘要" in x for x in c)]
    assert len(retry_calls) == 1, f"护栏生效时对话重试只应发生一次（实际 {len(retry_calls)}）"


@pytest.mark.asyncio
async def test_reactive_compact_terminates_after_exhausted_attempts() -> None:
    """契约：两次压缩重试均仍超长时必须设置 TerminalReason.PROMPT_TOO_LONG
    （死枚举激活）并返回 None 由调用方终止本轮。"""
    from bandi.agent.states import _reactive_compact_retry

    provider = _AlwaysTooLongProvider()
    ctx = _make_ctx(provider, _big_messages(10))
    ctx.raw_input = str(ctx.messages[1].content)

    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert result is None, "重试耗尽必须返回 None"
    assert ctx.terminal_reason == TerminalReason.PROMPT_TOO_LONG, (
        f"死枚举必须激活（got {ctx.terminal_reason}）"
    )
    assert ctx.final_message, "必须携带用户可读的终止说明"


class _AlwaysTooLongProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.call_count += 1
        raise _make_http_error(_LONG_ERROR_TEXTS[0])

    @property
    def protocol(self) -> str:
        return "openai"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.mark.asyncio
async def test_reactive_compact_stops_early_when_compression_ineffective() -> None:
    """契约（D-d1 护栏）：压缩未产生缩减时不得继续烧第二次 LLM 重试
    （无效缩减提前终止，防压缩循环成本）。"""
    from bandi.agent.states import _reactive_compact_retry

    provider = _AlwaysTooLongProvider()
    # 极少消息：pipeline 无可压内容 → compress 返回原样（stage=none 或未缩减）
    small = [
        Message(role=MessageRole.SYSTEM, content="sys"),
        Message(role=MessageRole.USER, content="hi"),
    ]
    ctx = _make_ctx(provider, small)
    ctx.raw_input = "hi"

    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert result is None
    assert ctx.terminal_reason == TerminalReason.PROMPT_TOO_LONG


@pytest.mark.asyncio
async def test_inner_layer_does_not_swallow_prompt_too_long(monkeypatch) -> None:
    """契约（破坏者视角，复查发现）：生产装配层 ``states._do_llm_call_inner`` 在
    use_fallback=True 时不得吞噬超长异常继续烧配置链/直调——CapabilityFallback
    短路上抛后必须立即冒泡到响应式压缩层（D-d2 止损在生产路径同样生效）。

    except 吞噬则止损失效——本用例钉住完整生产链路。"""
    from bandi.agent.states import _do_llm_call_inner

    calls: list[str] = []

    class _ShortCircuitFallback:
        def __init__(self, capability_registry, config) -> None:
            pass

        async def complete_with_fallback(self, **kwargs):
            calls.append("capability")
            raise _make_http_error(_LONG_ERROR_TEXTS[0])

    class _RecordingProvider:
        protocol = "openai"
        model_name = "fake"

        async def complete(self, messages, tools=None, **kw):
            calls.append("direct")

            class _R:
                content = "x"
                tool_calls = []
                finish_reason = "stop"

            return _R()

        def count_tokens(self, t):
            return max(1, len(t) // 4)

    monkeypatch.setattr("bandi.llm.strategy.CapabilityFallback", _ShortCircuitFallback)

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=_RecordingProvider(),
        config=AgentConfig(),
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.active_tools = []

    with pytest.raises(httpx.HTTPStatusError):
        await _do_llm_call_inner(
            ctx,
            [{"role": "user", "content": "hi"}],
            use_fallback=True,
            registry=None,
            bandi_config=SimpleNamespace(llm=SimpleNamespace(timeout=5)),
        )

    assert calls == ["capability"], (
        f"超长必须从 CapabilityFallback 直接冒泡（实际调用序列 {calls}——配置链/直调被白烧）"
    )
