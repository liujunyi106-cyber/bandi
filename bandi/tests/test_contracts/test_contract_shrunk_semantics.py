# 功能：压缩缩减口径契约——last_shrunk 必须按真实请求总量判定（压缩空转收口）
# 而压缩恰把内容搬进 SYSTEM 摘要消息：摘要比原文长时 compressible 反降 → 误判
# 「已缩减灾」→ 状态机回 thinking 续压空转。必须按 live（含 SYSTEM）前后比较。
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.states import on_compressing
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage


class _StubCountingProvider:
    """功能：provider 替身——complete 回放预设摘要，count_tokens 按 4 字符/Token 估算。"""

    def __init__(self, summary_text: str) -> None:
        self._summary_text = summary_text

    async def complete(self, messages: list[dict], tools: Any = None, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=self._summary_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def _messages(rounds: int) -> list[Message]:
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手")]
    for i in range(rounds):
        msgs.append(
            Message(role=MessageRole.USER, content=f"第{i}步执行日志：logs/step_{i}.py 校验通过")
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}步完成，进入下一步"))
    return msgs


def _ctx(messages: list[Message], provider: Any) -> Any:
    """功能：构造可直调 on_compressing 的最小 ExecutionContext（生产形态 pipeline）。"""
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import CompressionPipeline, ProgressiveCompressor

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=list(messages),
        provider=provider,
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    # 前置：total_tokens 接近窗口（pipeline.compress 的 stage 选择按此比例
    # 0 会误落维护档导致压缩前置失败）
    ctx.total_tokens = 190
    ctx.compress = CompressionState(
        pipeline=CompressionPipeline(
            progressive=ProgressiveCompressor(max_chunks=2, target_token_budget=10)
        )
    )
    return ctx


class TestShrunkSemantics:
    @pytest.mark.asyncio
    async def test_summary_inflation_not_counted_as_shrunk(self) -> None:
        """破坏者：超长摘要（总量未减甚至增大）不得标记已缩减——否则回 thinking 空转。

        口径不变量：last_shrunk 必须恒等于「压缩后 live < 压缩前 live」（含 SYSTEM
        摘要增量）；旧 compressible 口径排除 SYSTEM，摘要搬运至 SYSTEM 时判定失真。
        """
        inflated = "## 目标\n" + "背景填充内容与更多细节。" * 2000
        provider = _StubCountingProvider(inflated)
        ctx = _ctx(_messages(20), provider)
        before_live = ctx.live_token_count()

        await on_compressing(ctx)

        after_live = ctx.live_token_count()
        assert ctx.compress.last_shrunk == (after_live < before_live), (
            "last_shrunk 必须按 live 前后对比判定（含 SYSTEM 摘要增量），"
            f"before={before_live} after={after_live}"
        )

    @pytest.mark.asyncio
    async def test_real_reduction_counts_as_shrunk(self) -> None:
        """正常缩减（总量变小）必须标记 last_shrunk=True（续压评估路径可达）。"""
        provider = _StubCountingProvider("## 目标\n- 摘要")
        ctx = _ctx(_messages(20), provider)
        before_live = ctx.live_token_count()

        await on_compressing(ctx)

        after_live = ctx.live_token_count()
        assert after_live < before_live, "前置：短摘要必须使请求总量变小"
        assert ctx.compress.last_shrunk is True
        assert ctx.compress.last_shrunk == (after_live < before_live)
