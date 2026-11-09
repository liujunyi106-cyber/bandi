"""

钉住行为族：on_compressing 失败→截断兜底路径的语义——
1. fallback 后真实失败计数保留（compress_failed() >= 2 可达——状态机
   compressing→thinking 跳出压缩循环的原始设计意图，transitions.py 消费）
2. fallback 后 compress_ok=True 保留（下游处置确认，不误触发 terminate）
3. consecutive_fallbacks 连续计数（真实成功后清零）+ 阈值 loss_risk 告警
4. compression_attempts / compression_successes 统计如实
5. QueryState 快照携带 consecutive_fallbacks（健康度跨 checkpoint 保留）

生产调用路径族：on_compressing（COMPRESSING 唯一处理器，transitions.py:86
消费 compress_failed）+ to_query_state（checkpoint 持久化）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from bandi.core.message import Message, MessageRole


def _ctx_with_pipeline() -> Any:
    """功能：构造可直调 on_compressing 的最小 ExecutionContext（生产形态）。"""
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import CompressionPipeline

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            # maintenance 档（compressible 25 < 有效窗口 100*阈值 50）：失败走计数
            # 累计而非终态——autocompact 档失败即判 PROMPT_TOO_LONG（终态早退）
            # 届时由 terminal_reason 机制退出而非 compress_failed
            Message(role=MessageRole.USER, content="u" * 100),
        ],
        provider=_CountingProvider(),
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.total_tokens = 190
    ctx.compress = CompressionState(pipeline=CompressionPipeline())
    return ctx


class _CountingProvider:
    """功能：最小 provider 替身——count_tokens 按 4 字符/Token 估算。"""

    def count_tokens(self, text: str) -> int:
        return len(text or "") // 4


class _RecordingBus:
    """功能：最小事件总线替身——收集 emit 供断言。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def _make_deps(**overrides: Any) -> Any:
    from bandi.agent.deps import QueryDeps

    def _not_wired(name: str):
        async def _missing(ctx: object, *a: object, **k: object) -> object:
            raise AssertionError(f"QueryDeps.{name} 不应被调用")

        return _missing

    defaults: dict[str, Any] = {
        "call_model": _not_wired("call_model"),
        "autocompact": _not_wired("autocompact"),
        "microcompact": _not_wired("microcompact"),
        "new_id": lambda: "stub-fixed-id",
    }
    defaults.update(overrides)
    return QueryDeps(**defaults)


def _failed_outcome() -> Any:
    from bandi.agent.deps import CompactOutcome
    from bandi.memory.compression_pipeline import CompressionStats

    return CompactOutcome(messages=[], success=False, stats=CompressionStats(stage=""))


def _success_outcome(n_msgs: int = 2) -> Any:
    from bandi.agent.deps import CompactOutcome
    from bandi.memory.compression_pipeline import CompressionStats

    return CompactOutcome(
        messages=[Message(role=MessageRole.USER, content="kept")] * n_msgs,
        success=True,
        stats=CompressionStats(stage="autocompact"),
    )


# ── 1. fallback 保留失败计数（破坏者：连续 2 次失败后 compress_failed 可达）──


@pytest.mark.asyncio
async def test_fallback_preserves_failure_count_and_ok_flag() -> None:
    """契约（.1）：压缩失败走截断兜底后——真实失败计数必须保留累计
    （不得清零），同时 compress_ok=True 保留（下游处置确认、不误 terminate）。"""
    from bandi.agent.states import on_compressing

    ctx = _ctx_with_pipeline()

    async def fake_autocompact(c: Any) -> Any:
        return _failed_outcome()

    ctx.query_deps = _make_deps(microcompact=fake_autocompact)

    await on_compressing(ctx)
    await on_compressing(ctx)

    assert ctx.compress.compress_failed_count == 2, (
        f"fallback 后失败计数必须保留累计（实际 {ctx.compress.compress_failed_count}）"
        "——清零即 compress_failed() 永不可达，状态机跳出压缩循环机制失效"
    )
    assert ctx.compress_failed() is True, "连续 2 次失败后 compress_failed() 必须为 True"
    assert ctx.compress.compress_ok is True, "fallback 后 compress_ok 必须为 True（防误 terminate）"


@pytest.mark.asyncio
async def test_compress_failed_recovers_loop_exit_semantics() -> None:
    """契约（.1b 状态机语义）：compress_failed()（>=2）必须恢复原设计意图——
    compressing 状态下达阈值后状态机可转 thinking（transitions.py:86 条件可满足）。"""
    from bandi.agent.states import on_compressing

    ctx = _ctx_with_pipeline()

    async def fake_autocompact(c: Any) -> Any:
        return _failed_outcome()

    ctx.query_deps = _make_deps(microcompact=fake_autocompact)
    for _ in range(3):
        await on_compressing(ctx)

    # 达阈值后 transitions 条件 `not needs_compression() or compress_failed()`
    # 的 compress_failed 分支必须可满足
    assert ctx.compress_failed() is True


# ── 2. consecutive_fallbacks 连续计数 + 成功清零 ─────────────────────────────


@pytest.mark.asyncio
async def test_consecutive_fallbacks_counted_and_cleared_on_success() -> None:
    """契约（.2）：consecutive_fallbacks 连续 fallback 递增；真实压缩成功后
    清零。attempts/successes 统计如实。"""
    from bandi.agent.states import on_compressing

    ctx = _ctx_with_pipeline()

    outcomes = [_failed_outcome(), _failed_outcome(), _success_outcome()]

    async def fake_autocompact(c: Any) -> Any:
        return outcomes.pop(0)

    ctx.query_deps = _make_deps(microcompact=fake_autocompact)

    await on_compressing(ctx)
    await on_compressing(ctx)
    assert ctx.compress.consecutive_fallbacks == 2
    assert ctx.compress.compression_attempts == 2
    assert ctx.compress.compression_successes == 0

    await on_compressing(ctx)
    assert ctx.compress.consecutive_fallbacks == 0, "真实成功后连续 fallback 计数必须清零"
    assert ctx.compress.compression_attempts == 3
    assert ctx.compress.compression_successes == 1


# ── 3. 阈值 loss_risk 告警 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loss_risk_warning_emitted_at_threshold(caplog: pytest.LogCaptureFixture) -> None:
    """契约（.3）：连续 fallback 达阈值（3）必须发出 compression_loss_risk
    告警（结构化、含计数）——监控可观测，不再「压缩失败看起来像成功」。"""
    from bandi.agent.states import on_compressing

    ctx = _ctx_with_pipeline()

    async def fake_autocompact(c: Any) -> Any:
        return _failed_outcome()

    ctx.query_deps = _make_deps(microcompact=fake_autocompact)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await on_compressing(ctx)

    risk_records = [r for r in caplog.records if "compression_loss_risk" in r.getMessage()]
    assert risk_records, "连续 3 次 fallback 必须发出 compression_loss_risk 告警"
    assert any("3" in r.getMessage() for r in risk_records), "告警必须携带连续失败计数"


# ── 4. QueryState 快照携带健康度 ──────────────────────────────────────────────


def test_query_state_carries_consecutive_fallbacks() -> None:
    """契约（.4）：consecutive_fallbacks 必须进 QueryState 快照（跨 checkpoint
    保留健康度——恢复后监控不失忆）；attempts/successes 为运行统计可 transient。"""
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import CompressionPipeline

    ctx = ExecutionContext(
        messages=[],
        provider=_CountingProvider(),
        config=AgentConfig(),
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.compress = CompressionState(pipeline=CompressionPipeline())
    ctx.compress.consecutive_fallbacks = 2

    qs = ctx.to_query_state()
    assert qs.consecutive_fallbacks == 2, "QueryState 快照必须携带 consecutive_fallbacks"


# ── 5. 失败兜底产物可执行性（truncate + 锚点） ────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_truncate_keeps_executable_and_injects_anchor() -> None:
    """契约（.5）：压缩失败走截断兜底后——SYSTEM 区全量保留、无空消息
    尾部注入 continuity 锚点（synthetic USER）。产物必须能直接继续喂 LLM。"""
    from bandi.agent.states import on_compressing

    ctx = _ctx_with_pipeline()

    async def fake_autocompact(c: Any) -> Any:
        return _failed_outcome()

    ctx.query_deps = _make_deps(microcompact=fake_autocompact)

    await on_compressing(ctx)

    msgs = ctx.messages
    assert msgs, "截断兜底后上下文不得为空"
    assert all(m.content for m in msgs), "可执行上下文不得含空内容消息"
    assert msgs[0].role == MessageRole.SYSTEM, "truncate 必须全量保留 SYSTEM 区"
    tail = msgs[-1]
    assert tail.role == MessageRole.USER and tail.meta.get("synthetic") is True, (
        "失败兜底必须注入 continuity 锚点（synthetic USER）保续作连续性"
    )
    orphans = [
        m
        for m in msgs
        if m.role == MessageRole.TOOL
        and not any(
            t.role == MessageRole.ASSISTANT and getattr(t, "tool_call_id", None) == m.tool_call_id
            for t in msgs
        )
    ]
    assert not orphans, "截断必须规避孤儿 TOOL（assistant 伙伴被裁=API 畸形）"
