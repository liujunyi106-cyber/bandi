"""QueryDeps 把 Agent Loop 的全部外部效果收敛为四个可替换依赖：

- ``call_model`` ：LLM 调用（预算/tracer/fallback/流式/响应式压缩全链在生产实现内）
- ``autocompact`` ：全历史压缩（正常档）——先试零 LLM 会话记忆压缩路径
                     CompressionPipeline 可选 LLM 摘要
- ``microcompact`` ：维护档免费压缩（prune/micro，零 LLM）
- ``new_id`` ：确定性 id 生成（checkpoint id）

测试注入 stub 即可拦截对应路径；未注入时 :func:`get_query_deps` 惰性构建
生产默认并回填缓存，生产行为与直接调用完全一致。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bandi.core.message import LLMResponse, Message

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bandi.agent.base import ExecutionContext


@dataclass(frozen=True)
class CompactOutcome:
    # 功能：压缩依赖统一产物——messages/success/stats 供 states 编排层消费
    messages: list[Message]
    success: bool
    stats: Any  # CompressionStats（Any 防运行时 import 环；生产路径恒为该类型）


@runtime_checkable
class CallModel(Protocol):
    # 功能：LLM 调用效果协议——实现方负责完整生产链（含 fallback/流式/响应式压缩）
    async def __call__(
        self, ctx: ExecutionContext, messages: list[dict[str, Any]]
    ) -> LLMResponse: ...


@runtime_checkable
class AutoCompact(Protocol):
    # 功能：全历史压缩效果协议（正常档）
    async def __call__(self, ctx: ExecutionContext) -> CompactOutcome: ...


@runtime_checkable
class MicroCompact(Protocol):
    # 功能：维护档免费压缩效果协议（prune/micro，禁止 LLM 调用）
    async def __call__(self, ctx: ExecutionContext) -> CompactOutcome: ...


@runtime_checkable
class NewId(Protocol):
    # 功能：id 生成效果协议（hex 字符串，如 checkpoint id）
    def __call__(self) -> str: ...


@dataclass(frozen=True)
class QueryDeps:
    # 功能：Agent Loop 四外部效果打包——测试打桩与生产装配的唯一边界
    call_model: CallModel
    autocompact: AutoCompact
    microcompact: MicroCompact
    new_id: NewId


def get_query_deps(ctx: ExecutionContext) -> QueryDeps:
    # 功能：取 ctx 的查询依赖——未注入时惰性构建生产默认并回填缓存
    if ctx.query_deps is None:
        ctx.query_deps = make_default_deps()
    assert ctx.query_deps is not None
    return ctx.query_deps


def make_default_deps() -> QueryDeps:
    # 功能：构建生产默认四依赖——薄包装既有调用链（行为与 DI 接线前一致）
    return QueryDeps(
        call_model=_production_call_model,
        autocompact=_production_autocompact,
        microcompact=_production_microcompact,
        new_id=_production_new_id,
    )


async def _production_call_model(
    ctx: ExecutionContext, messages: list[dict[str, Any]]
) -> LLMResponse:
    # 功能：生产 call_model——use_fallback 判定下沉至生产实现（判定无副作用，时序不变）
    from bandi.agent import states

    registry = ctx.meta.get("provider_registry")
    bandi_config = ctx.meta.get("bandi_config")
    use_fallback = registry is not None and bandi_config is not None and bandi_config.llm
    purpose = str(ctx.meta.get("_llm_call_purpose", "chat"))
    started = time.perf_counter()
    response = await states._do_llm_call(
        ctx,
        messages,
        use_fallback,
        registry,
        bandi_config,
        allow_reactive_compact=purpose == "chat" and not ctx.meta.get("_reactive_retry_active"),
        allow_stream=purpose == "chat",
    )
    # 功能：每次模型调用记录耗时与 token 分项，用于定位慢轮次与累计 token 偏高
    usage = getattr(response, "usage", None)
    logger.info(
        "llm call: purpose=%s msgs=%d prompt_tokens=%s completion_tokens=%s "
        "session_total=%d duration_ms=%.0f finish=%s",
        purpose,
        len(messages),
        getattr(usage, "prompt_tokens", 0) if usage else 0,
        getattr(usage, "completion_tokens", 0) if usage else 0,
        int(getattr(ctx, "total_tokens", 0) or 0),
        (time.perf_counter() - started) * 1000.0,
        getattr(response, "finish_reason", "") or "",
    )
    return response


def _ensure_pipeline(ctx: ExecutionContext) -> Any:
    # 功能：跨轮复用 CompressionPipeline（token 历史保留，fast_growth 生效）
    from bandi.memory.compression_pipeline import CompressionPipeline

    if ctx.compress.pipeline is None:
        ctx.compress.pipeline = CompressionPipeline()
    return ctx.compress.pipeline


async def _compress_via_pipeline(ctx: ExecutionContext, *, maintenance: bool) -> CompactOutcome:
    # 功能：生产压缩公共体——同一 CompressionPipeline 出口，按档位分发
    pipeline = _ensure_pipeline(ctx)

    async def complete_for_compaction(messages: list[dict[str, Any]]) -> LLMResponse:
        previous = ctx.meta.get("_llm_call_purpose")
        ctx.meta["_llm_call_purpose"] = "compaction"
        try:
            return await get_query_deps(ctx).call_model(ctx, messages)
        finally:
            if previous is None:
                ctx.meta.pop("_llm_call_purpose", None)
            else:
                ctx.meta["_llm_call_purpose"] = previous

    # 1.3：策略选择/历史用 live 口径（messages + tools 的真实当前规模）
    # ctx.total_tokens 是会话累计成本口径，混用会随历史近平方增长虚高。
    # 防御式取值：测试替身（SimpleNamespace 最小 ctx 形态）无新 helper 时回退旧字段
    live_tokens = (
        ctx.live_token_count() if hasattr(ctx, "live_token_count") else int(ctx.total_tokens)
    )
    window = getattr(ctx, "effective_context_window", None)
    if window is None:
        window = int(getattr(getattr(ctx, "config", None), "context_window", 0) or 0)
    read_state = getattr(getattr(ctx, "tool_executor", None), "read_state", None)
    compressed, success, _, stats = await pipeline.compress(
        ctx.messages,
        live_tokens,
        window,
        ctx.config.compression_threshold,
        complete_for_compaction,
        maintenance=maintenance,
        read_state=read_state,
    )
    return CompactOutcome(messages=compressed, success=success, stats=stats)


async def _production_autocompact(ctx: ExecutionContext) -> CompactOutcome:
    # （working scratchpad 快照），缺失/无快照回退
    # 传统 CompressionPipeline（正常档，可选 LLM 摘要）
    from bandi.memory.session_memory import try_session_compact

    sm_outcome = await try_session_compact(ctx)
    if sm_outcome is not None:
        return sm_outcome
    return await _compress_via_pipeline(ctx, maintenance=False)


async def _production_microcompact(ctx: ExecutionContext) -> CompactOutcome:
    # 功能：生产 microcompact——维护免费档（强制 prune/micro，零 LLM）
    return await _compress_via_pipeline(ctx, maintenance=True)


def _production_new_id() -> str:
    # 功能：生产 id 生成——uuid4 hex
    from uuid import uuid4

    return uuid4().hex
