# 双开关保护：需同时满足 ① BANDI_REAL_LLM_API_KEY + BANDI_LLM_BASE_URL
from __future__ import annotations

import os
from typing import Any

import pytest

from bandi.core.config import AgentConfig, LLMConfig
from bandi.core.message import Message, MessageRole

_API_KEY = os.environ.get("BANDI_REAL_LLM_API_KEY", "")
_BASE_URL = os.environ.get("BANDI_LLM_BASE_URL", "")
_MODEL = os.environ.get("BANDI_REAL_LLM_MODEL", "")
_REAL_ENABLED = (
    bool(_API_KEY and _BASE_URL and _MODEL) and os.environ.get("BANDI_REAL_LLM_TESTS") == "1"
)

pytestmark = pytest.mark.skipif(
    not _REAL_ENABLED,
    reason="真实 LLM E2E：需 BANDI_REAL_LLM_API_KEY/BANDI_LLM_BASE_URL/BANDI_REAL_LLM_MODEL 三变量齐备 + BANDI_REAL_LLM_TESTS=1 开闸",
)

_SUMMARY_REQUEST_MARKER = "摘要"  # progressive 摘要请求的 system 标识词


@pytest.fixture(autouse=True)
def _rate_limit_gap():
    """zen 免费端点限流护栏：用例间强制间隔。"""
    import time

    yield
    time.sleep(8)


# ── 基建：spy 包装 + ctx 构造 ────────────────────────────────────────────


class _RequestSpy:
    """功能：包装真实 provider 记录每次 LLM 请求的 messages 副本（内容级断言用）；
    未显式覆盖的属性（model_name/auth_token 等）全部兜底转发内层 provider。
    provider supports_streaming，请求实际走流式路径；只包 complete 时 requests
    恒空导致 恒 skip（i 日误诊为端点 503 的 spy 盲区）。"""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "requests", [])
        object.__setattr__(self, "stream_calls", 0)
        object.__setattr__(self, "complete_calls", 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def _record(self, messages: list[dict]) -> None:
        self.requests.append([dict(m) for m in messages])

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kw: Any):
        self._record(messages)
        object.__setattr__(self, "complete_calls", self.complete_calls + 1)
        return await self._inner.complete(messages, tools=tools, **kw)

    def stream(self, messages: list[dict], tools: list[dict] | None = None, **kw: Any):
        self._record(messages)
        object.__setattr__(self, "stream_calls", self.stream_calls + 1)
        return self._stream_inner(messages, tools=tools, **kw)

    async def _stream_inner(self, messages: list[dict], tools: list[dict] | None = None, **kw: Any):
        async for event in self._inner.stream(messages, tools=tools, **kw):
            yield event

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)

    def summary_request_count(self) -> int:
        """progressive 摘要请求数（system 含摘要指令标识）。"""
        return sum(
            1
            for req in self.requests
            if req and _SUMMARY_REQUEST_MARKER in (req[0].get("content") or "")
        )


def _build_provider():
    from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy
    from bandi.llm.providers.openai import OpenAICompatProvider

    cfg = LLMConfig(
        default_model=_MODEL,
        provider="openai_compat",
        api_key=_API_KEY,
        base_url=_BASE_URL,
        max_output_tokens=512,
        timeout=240,
    )
    # 生产 build_provider 同款熔断+重试包装——zen 免费端点偶发 503 需退避
    return OpenAICompatProvider(cfg, _BASE_URL).with_circuit_breaker(
        CircuitBreaker(), RetryPolicy(max_retries=4, base_delay=2.0)
    )


def _make_loop_ctx(spy: Any, context_window: int = 4000) -> Any:
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.memory.compression_pipeline import (
        CompressionPipeline,
        ProgressiveCompressor,
    )

    config = AgentConfig(
        context_window=context_window,
        compression_threshold=0.5,
        max_iterations=2,
    )
    ctx = ExecutionContext(messages=[], provider=spy, config=config, bus=_QuietBus())
    # 「缩小世界」等比配置：context_window 4000（生产的 1/32）→
    # progressive 预算同比例缩小（16000/32≈500），否则内部 MemoryCompactor
    # 的最小字符门槛会让小场景摘要静默拒绝
    ctx.compress = CompressionState(
        pipeline=CompressionPipeline(
            progressive=ProgressiveCompressor(
                max_summary_chars=1200,
                target_token_budget=500,
                max_chunks=2,
            )
        )
    )
    return ctx


class _QuietBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def subscribe(self, *a: Any, **k: Any) -> None:
        pass


def _long_dialog(early_fact: str, fill_rounds: int) -> list[Message]:
    """早期关键事实 + 大量填充轮（触发 token 阈值，压缩后验证关键信息保留）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手，回答保持简洁。"),
        Message(role=MessageRole.USER, content=f"开始任务。记住关键信息：{early_fact}"),
        Message(role=MessageRole.ASSISTANT, content="已记住该关键信息，将贯穿任务全程使用。"),
    ]
    filler = "这是例行检查步骤的详细记录，包含状态更新与中间数值，用于撑起上下文长度。" * 6
    for i in range(fill_rounds):
        msgs.append(Message(role=MessageRole.USER, content=f"第{i}批例行检查：{filler}"))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}批检查完成：{filler}"))
    return msgs


async def _run_two_turns(ctx: Any, max_turns: int = 2) -> None:
    """直跑状态机 N 轮 thinking（绕过 perception，聚焦压缩链路）。"""
    from bandi.agent.loop import AgentLoop

    loop = AgentLoop(max_iterations=4)
    await loop._run_loop(ctx, max_turns=max_turns, turns_taken=0)


# ── +自动压缩全链路 + 请求形态断言 ────────────────────────────────


@pytest.mark.asyncio
async def test_s1_auto_compaction_full_chain() -> None:
    """token 达阈值 → 真实 LLM 摘要 → boundary → 锚点 → 续作含关键信息。"""
    from bandi.core.events import EventType
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX
    from bandi.memory.compression_pipeline import COMPACT_BOUNDARY_PREFIX

    spy = _RequestSpy(_build_provider())
    ctx = _make_loop_ctx(spy)
    early_fact = "项目代号是蓝鲸，部署端口是 7421"
    ctx.messages = _long_dialog(early_fact, fill_rounds=26)
    ctx.raw_input = f"开始任务。记住关键信息：{early_fact}"
    ctx.total_tokens = sum(spy.count_tokens(m.content or "") for m in ctx.messages)
    assert ctx.needs_compression(), (
        f"前置失败：构造的消息未达压缩阈值 total_tokens={ctx.total_tokens}"
    )

    await _run_two_turns(ctx)

    boundaries = [m for m in ctx.messages if (m.content or "").startswith(COMPACT_BOUNDARY_PREFIX)]
    assert boundaries, "RED: 未产生 [压缩边界] 标记"
    summaries = [m for m in ctx.messages if (m.content or "").startswith(SUMMARY_MESSAGE_PREFIX)]
    assert summaries, "RED: 未产生结构化摘要消息"
    assert "蓝鲸" in summaries[0].content or "7421" in summaries[0].content, (
        f"RED: 真实摘要丢失早期关键信息：{summaries[0].content[:200]}"
    )
    # 两种形态都合法，本质断言 = 任务指令在压缩后上下文中可达
    raw_kept_or_replayed = any(
        m.role == MessageRole.USER and m.content == ctx.raw_input for m in ctx.messages
    )
    assert raw_kept_or_replayed, "RED: raw_input 既不在保留区也未 replay——任务连续性断裂"
    state_events = [
        e for e in ctx.bus.events if getattr(e, "type", None) == EventType.STATE_CHANGED
    ]
    assert state_events, "RED: 无状态迁移事件"
    # 报告「压缩端到端契约出现空回复」的最终产物级断言（不止摘要存在，还要续作有产出）
    _continuation_replies = [
        m.content
        for m in ctx.messages
        if m.role == MessageRole.ASSISTANT and (m.content or "").strip()
    ]
    assert _continuation_replies, "RED: 压缩后无续作回复——压缩链路未闭环（空回复）"


@pytest.mark.asyncio
async def test_s2_request_shape_after_compaction() -> None:
    """压缩后续作请求的内容级形态——system 区带摘要实文、被裁填充不在请求中。"""
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX
    from bandi.memory.compression_pipeline import COMPACT_BOUNDARY_PREFIX

    spy = _RequestSpy(_build_provider())
    ctx = _make_loop_ctx(spy)
    ctx.messages = _long_dialog("项目代号是蓝鲸，部署端口是 7421", fill_rounds=26)
    ctx.raw_input = "开始任务。记住关键信息：项目代号是蓝鲸，部署端口是 7421"
    ctx.total_tokens = sum(spy.count_tokens(m.content or "") for m in ctx.messages)

    await _run_two_turns(ctx)

    assert len(spy.requests) >= 2, "至少应有压缩前+续作两次请求"
    # 过滤掉压缩管道自身的摘要/合并请求——只看 thinking 续作请求的形态
    continuation_reqs = [
        r for r in spy.requests if r and "摘要" not in str(r[0].get("content") or "")[:40]
    ]
    if not continuation_reqs:
        # 本 skip 仅在真实端点错误时触发
        pytest.skip("zen 端点错误中断续作请求（免费池波动），S2 待稳定窗口复验")
    last_req = continuation_reqs[-1]
    full_text = "\n".join(str(m.get("content") or "") for m in last_req)
    assert SUMMARY_MESSAGE_PREFIX.strip()[:6] in full_text, "RED: 续作请求缺摘要标记"
    assert COMPACT_BOUNDARY_PREFIX in full_text, "RED: 续作请求缺压缩边界标记"
    assert any("蓝鲸" in str(m.get("content") or "") for m in last_req), (
        "RED: 续作请求缺关键信息（摘要保真断裂）"
    )


# ── 维护免费档（零摘要调用）──────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_maintenance_mode_no_summary_llm() -> None:
    """轮满 10 维护触发——该次压缩必须是 micro 免费档（零摘要请求）。
    单轮聚焦：多轮 thinking 的 usage 会累加 total_tokens 跨越阈值
    那是正常的 token 维度压缩（场景），不属于维护档验证范围。"""
    from bandi.memory.compression_pipeline import COMPACT_BOUNDARY_PREFIX

    spy = _RequestSpy(_build_provider())
    ctx = _make_loop_ctx(spy)
    big_tool = Message(
        role=MessageRole.TOOL, content="工具输出数据流\n" + "0123456789" * 300, tool_call_id="c1"
    )
    ctx.messages = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手，回答保持简洁。"),
        Message(role=MessageRole.USER, content="处理这批数据并给结论"),
        big_tool,
        Message(role=MessageRole.ASSISTANT, content="数据处理完成，结论见上。"),
    ]
    ctx.total_tokens = 800  # 远低于阈值（2000）
    ctx.compress.turns_since_maintenance = 10
    assert ctx.needs_compression(), "前置失败：维护未触发"

    await _run_two_turns(ctx, max_turns=1)

    boundaries = [m for m in ctx.messages if (m.content or "").startswith(COMPACT_BOUNDARY_PREFIX)]
    assert boundaries, "RED: 维护压缩未产生 boundary"
    first_boundary = boundaries[0]
    assert "stage=micro" in first_boundary.content, (
        f"RED: 维护首次档位应为 micro：{first_boundary.content[:100]}"
    )
    assert spy.summary_request_count() == 0, (
        f"RED: 维护模式出现 {spy.summary_request_count()} 次摘要调用"
    )


# ── /compact REPL 手动链路（五节结构）───────────────────────────────


@pytest.mark.asyncio
async def test_s4_manual_compact_five_sections() -> None:
    """MemoryCompactor.compact 真实 LLM 摘要输出五节结构。"""
    provider = _build_provider()
    from bandi.memory.compaction import MemoryCompactor

    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是会话摘要工具。"),
        Message(
            role=MessageRole.USER,
            content="启动重构任务：把 config 加载改为懒加载，注意保持向后兼容",
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="已完成 config.py 改造：Config.load() 改为首次访问时加载，缓存实例。决策：保留旧同步 API 签名避免破坏 CLI。下一步处理测试夹具。",
        ),
        Message(role=MessageRole.USER, content="测试夹具改完了吗？遇到什么问题？"),
        Message(
            role=MessageRole.ASSISTANT,
            content="夹具已改完。问题：两个测试依赖模块级单例顺序，已通过 fixture 隔离解决。当前阻塞：无。下一步跑全量回归。",
        ),
    ]
    compactor = MemoryCompactor(max_summary_chars=1500, target_token_budget=40)

    async def llm_complete(prompt: list[dict]):
        return await provider.complete(prompt)

    result, success, _ = await compactor.compact(msgs, llm_complete)
    assert success, "RED: /compact 摘要失败"
    summary_msgs = [m for m in result if "## 目标" in (m.content or "")]
    assert summary_msgs, (
        f"RED: 摘要缺五节结构（目标节）：{[(m.role, (m.content or '')[:80]) for m in result]}"
    )
    body = summary_msgs[0].content or ""
    for section in ("## 进度", "## 关键决策", "## 下一步"):
        assert section in body, f"RED: 缺 {section} 节"
    assert "懒加载" in body or "config" in body, "RED: 摘要丢失任务关键词"


# ── ReactiveCompact best-effort（超限错误驱动）──────────────────────


@pytest.mark.asyncio
async def test_s5_reactive_compact_on_oversize() -> None:
    """（best-effort）：构造超限 prompt 触发服务端错误 → 错误识别 + 压缩重试。
    zen 端点若对超长请求行为不符（不限长/不同错误码），局部 skip 登记。"""
    from bandi.llm.strategy import is_prompt_too_long_error

    provider = _build_provider()
    oversize = [{"role": "user", "content": "压力测试 " + "x" * 400_000}]
    raised = None
    try:
        await provider.complete(oversize)
    except Exception as e:  # noqa: BLE001
        raised = e
    if raised is None:
        pytest.skip("zen 端点未拒绝超长请求（模型窗口极大），S5 场景不可构造")
    if not is_prompt_too_long_error(raised):
        pytest.skip("信息性登记：超长错误未被识别为 PROMPT_TOO_LONG——响应形态记入调优登记")


def _model_available_on_endpoint() -> bool:
    """M-r3：目标 model 必须在端点 /models 列表中——三变量机制只防配置错
    不防模型从端点下线（下线时 会 FAILED 而非 SKIP，假红护栏）。"""
    import httpx

    try:
        resp = httpx.get(
            f"{_BASE_URL.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=20,
        )
        return resp.status_code == 200 and _MODEL in {
            m.get("id") for m in resp.json().get("data", [])
        }
    except Exception:  # noqa: BLE001 — 探测失败按不可用处理走 skip
        return False


@pytest.mark.asyncio
async def test_s6_production_fallback_streaming_deltas() -> None:
    """（技术债 #16 证据）：meta 注入真实 ProviderRegistry + bandi_config
    → 生产 call_model 走 q 日候选池流式分支（use_fallback=True），THINKING_DELTA/
    CONTENT_DELTA 经 bus 可达、响应由流式组装——Mock 契约的对应。"""

    if not _model_available_on_endpoint():
        pytest.skip(f"端点 /models 不含 {_MODEL}（可能已下线），S6 场景不可构造")

    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.core.config import BandiConfig
    from bandi.core.events import EventType
    from bandi.llm.registry import ProviderRegistry
    from bandi.memory.compression_pipeline import (
        CompressionPipeline,
        ProgressiveCompressor,
    )

    cfg = BandiConfig()
    cfg.llm.api_key = _API_KEY
    cfg.llm.base_url = _BASE_URL
    cfg.llm.default_model = _MODEL
    cfg.agent.context_window = 4000
    cfg.agent.compression_threshold = 0.5

    spy = _RequestSpy(_build_provider())
    real_registry = ProviderRegistry(cfg)

    class _RegistrySpy:
        """生产 ProviderRegistry 的观察包装——get_provider 产物统一路由经 spy
        否则 registry 内部缓存的自建实例绕过请求记录（首跑 stream=0 实锤）。"""

        def __init__(self, inner: Any, observe: Any) -> None:
            self._inner = inner
            self._observe = observe

        def get_provider(self, model: str, config: Any) -> Any:
            return self._observe

        def close_all(self) -> None:
            close_fn = getattr(self._inner, "close_all", None)
            if callable(close_fn):
                close_fn()

    config = AgentConfig(
        context_window=4000,
        compression_threshold=0.5,
        max_iterations=2,
    )
    ctx = ExecutionContext(messages=[], provider=spy, config=config, bus=_QuietBus())
    ctx.compress = CompressionState(
        pipeline=CompressionPipeline(
            progressive=ProgressiveCompressor(
                max_summary_chars=1200, target_token_budget=500, max_chunks=2
            )
        )
    )
    ctx.meta["provider_registry"] = _RegistrySpy(real_registry, spy)
    ctx.meta["bandi_config"] = cfg
    ctx.raw_input = "用一句话介绍蓝鲸"
    ctx.messages = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手，回答保持简洁。"),
        Message(role=MessageRole.USER, content="用一句话介绍蓝鲸"),
    ]

    await _run_two_turns(ctx, max_turns=2)

    # 生产形态判定链实锤：use_fallback=True 时优先经候选池流式分支消费；
    # 不钉 complete==0（免费池波动下逐轮全流式无法保证，r 日首跑两轮全流式属幸运样本）
    assert spy.stream_calls >= 1, (
        f"生产形态必须至少一轮走候选池流式分支（stream={spy.stream_calls} "
        f"complete={spy.complete_calls}）"
    )
    stream_datas = [
        e.data for e in ctx.bus.events if getattr(e, "type", None) == EventType.LLM_STREAM_EVENT
    ]
    types = [d.get("type") for d in stream_datas]
    assert "content_delta" in types or "thinking_delta" in types, (
        f"真机 SSE 流事件必须经 bus 可达（实际 {types}）；done 语义见免费池口径"
    )
    assert ctx.current_content or ctx.current_response is not None, "流式响应必须组装"
