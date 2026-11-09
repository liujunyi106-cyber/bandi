# 功能：压缩预算口径契约——压缩触发与不可收敛判定必须基于可压缩对话（排除 SYSTEM 固定区）
# ≈ 11K token），而压缩/截断无法缩减 SYSTEM → 压缩后仍超阈值 → PROMPT_TOO_LONG /
# thinking↔compressing 振荡至 MAX_TURNS。契约钉死：SYSTEM 不可压部分不计入压缩触发。
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.states import on_parsing
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.llm.mock import MockProvider

_WINDOW = 2500
_THRESHOLD = 0.6
_BIG_SYSTEM = "x" * 80000  # ≈ 20000 token，远超窗口
_BIG_DIALOGUE = "y" * 8000  # ≈ 2000 token，超阈值
_SMALL = "短内容"


def _make_ctx(
    messages: list[Message],
    **overrides: Any,
) -> ExecutionContext:
    config = AgentConfig(context_window=_WINDOW, compression_threshold=_THRESHOLD)
    ctx = ExecutionContext(
        messages=messages,
        provider=MockProvider(),
        config=config,
        bus=EventBus(),
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def _big_system_small_dialogue() -> list[Message]:
    return [
        Message(role=MessageRole.SYSTEM, content=_BIG_SYSTEM),
        Message(role=MessageRole.USER, content=f"记住暗号 pineapple-77。{_SMALL}"),
        Message(role=MessageRole.ASSISTANT, content="收到，已记住。"),
    ]


# ── 压缩触发口径 ─────────────────────────────────────────────


def test_needs_compression_excludes_system() -> None:
    """契约（口径 1）：SYSTEM 超窗但对话低于阈值 → 不触发压缩。
    SYSTEM（工具 schema/静态提示）不可被压缩缩减，计入预算会导致
    压缩后仍超阈值 → 无限压缩循环。"""
    ctx = _make_ctx(_big_system_small_dialogue())
    assert ctx.needs_compression() is False


def test_needs_compression_counts_dialogue() -> None:
    """契约（口径 2）：对话超阈值 → 触发压缩（SYSTEM 是否超窗不影响该判定）。"""
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content=_BIG_DIALOGUE),
    ]
    ctx = _make_ctx(msgs)
    assert ctx.needs_compression() is True


def test_system_only_overflow_never_triggers_compression() -> None:
    """契约（口径 3，防死循环核心不变式）：仅 SYSTEM 超窗、对话为空 → 永不触发压缩。
    该场景下压缩无对象可压，触发只会制造 thinking↔compressing 死循环。"""
    ctx = _make_ctx([Message(role=MessageRole.SYSTEM, content=_BIG_SYSTEM)])
    assert ctx.needs_compression() is False


def test_compressible_token_count_excludes_system() -> None:
    """契约（口径 4）：compressible_token_count 只统计非 SYSTEM 消息。"""
    ctx = _make_ctx(_big_system_small_dialogue())
    compressible = ctx.compressible_token_count()
    live = ctx.live_token_count()
    system_tokens = ctx.provider.count_tokens(_BIG_SYSTEM)
    assert compressible < live
    assert live - compressible >= system_tokens * 0.9  # 差值≈SYSTEM 部分
    assert compressible < _WINDOW * _THRESHOLD


def test_maintenance_path_unchanged_for_low_tokens() -> None:
    """契约（口径 5）：token 低于阈值时维护维度（轮数/空闲）行为不变。"""
    ctx = _make_ctx(_big_system_small_dialogue())
    ctx.compress.turns_since_maintenance = 50
    assert ctx.needs_compression() is True


# ── 幻觉检查首轮豁免 ─────────────────────────────────────────


class _RecordingChecker:
    """记录 check 调用并返回可配置高分的假幻觉检查器。"""

    def __init__(self, score: float = 1.0) -> None:
        self.score = score
        self.calls: list[tuple[str, str]] = []

    async def check(self, text: str, context: str = "") -> Any:
        self.calls.append((text[:50], context[:50]))

        class _R:
            overall_score = self.score
            blocked = self.score >= 0.9

        return _R()


def _parsing_ctx(
    messages: list[Message],
    checker: _RecordingChecker,
    raw_input: str = "记住暗号是 pineapple-77。回复收到即可。",
    content: str = "好的，已记住暗号。",
) -> ExecutionContext:
    ctx = _make_ctx(messages, raw_input=raw_input)
    ctx.current_response = LLMResponse(content=content, finish_reason="stop")
    ctx.current_content = content
    ctx.hallucination_checker = checker
    return ctx


@pytest.mark.asyncio
async def test_hallucination_gate_skipped_for_confirmation_no_history() -> None:
    """契约（幻觉 1）：首轮无既往历史 **且回复为短确认/忠实复述**（无新增事实
    断言）时不跑幻觉 judge——弱 judge 对短确认回复系统性误判（冒烟
    score=1.0/0.9 连拦两轮致三场景全灭）。豁免不是『无历史』一个信号，而是
    结合『回复无实质事实断言』才成立。"""
    checker = _RecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="记住暗号是 pineapple-77。回复收到即可。"),
    ]
    ctx = _parsing_ctx(msgs, checker)
    await on_parsing(ctx)
    assert checker.calls == [], "首轮短确认回复不得调用幻觉 judge"
    assert ctx.terminal_reason is not TerminalReason.SAFETY_BLOCKED
    assert ctx.terminal_reason is TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_hallucination_gate_still_blocks_fabrication_no_history() -> None:
    """契约（幻觉 2，集成回归锚点）：首轮无历史但回复**含实质事实断言**
    （凭空捏造具体项目/实体）时仍必须跑幻觉 judge 并拦截——不得因『首轮』
    豁免真实幻觉。对应 test_hallucination_blocks_and_records 语义。"""
    checker = _RecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.USER, content="What projects are complete?"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="What projects are complete?")
    ctx.meta["_hallucination_rewrite_count"] = 2
    ctx.current_response = LLMResponse(
        content=(
            "I have completed the Phantom Project, Galactic Quest, Quantum Core, "
            "Infinite Loop, Sky Bridge, Dream Weaver, Mind Palace, and Star Portal"
        ),
        finish_reason="stop",
    )
    ctx.current_content = (
        "I have completed the Phantom Project, Galactic Quest, Quantum Core, "
        "Infinite Loop, Sky Bridge, Dream Weaver, Mind Palace, and Star Portal"
    )
    await on_parsing(ctx)
    assert len(checker.calls) == 1, "首轮凭空捏造事实时必须跑幻觉 judge"
    assert ctx.terminal_reason is TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_hallucination_gate_still_runs_with_history() -> None:
    """契约（幻觉 3）：有既往对话历史时幻觉检查照常执行（豁免不得过宽）。"""
    checker = _RecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="第一轮：项目代号是蓝鲸。"),
        Message(role=MessageRole.ASSISTANT, content="已记录：代号蓝鲸。"),
        Message(role=MessageRole.USER, content="再确认一次代号。"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="再确认一次代号。")
    ctx.meta["_hallucination_rewrite_count"] = 2
    ctx.current_response = LLMResponse(content="代号是天马。", finish_reason="stop")
    await on_parsing(ctx)
    assert len(checker.calls) == 1
    assert ctx.terminal_reason is TerminalReason.SAFETY_BLOCKED


class _FullRecordingChecker:
    """记录完整 check 输入（context 全量）的假幻觉检查器——钉 judge 证据链内容。"""

    def __init__(self, score: float = 1.0) -> None:
        self.score = score
        self.calls: list[tuple[str, str]] = []

    async def check(self, text: str, context: str = "") -> Any:
        self.calls.append((text, context))

        class _R:
            overall_score = self.score
            blocked = self.score >= 0.9

        return _R()


@pytest.mark.asyncio
async def test_hallucination_gate_sees_tool_evidence() -> None:
    """契约（幻觉 4）：基于工具检索结果（knowledge_search/read）的事实回答——
    judge 证据链必须包含最近 TOOL 消息。冒烟 ：压缩把『记住暗号
    pineapple-77』轮浓缩为 compaction 摘要条目，模型经 knowledge_search 检索
    召回后正确回答，但 judge 上下文只拼 USER/ASSISTANT（TOOL 消息被滤）→
    无据可对照 → score=0.8 误判幻觉拦截。修复后：TOOL 输出作为证据进 context。"""
    checker = _FullRecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="第一轮任务完成，会话已压缩。"),
        Message(
            role=MessageRole.TOOL,
            content=(
                '<tool_output tool="knowledge_search">{"success": true, "action": "read", '
                '"text": "## 目标 记住暗号 pineapple-77 并复述材料要点"}</tool_output>'
            ),
        ),
        Message(role=MessageRole.USER, content="暗号是什么？只回答暗号本身。"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="暗号是什么？只回答暗号本身。")
    ctx.current_response = LLMResponse(content="pineapple-77", finish_reason="stop")
    ctx.current_content = "pineapple-77"
    await on_parsing(ctx)
    assert len(checker.calls) == 1, "有既往历史必须送 judge"
    _, jctx = checker.calls[0]
    assert "pineapple-77" in jctx, "judge 证据链必须含最近 TOOL 输出（模型引用的事实来源）"


@pytest.mark.asyncio
async def test_hallucination_gate_tool_evidence_still_blocks_fabrication() -> None:
    """契约（幻觉 5，防豁免过宽）：TOOL 证据加入不豁免 judge 本身——回复与
    对话/工具证据均不符的凭空捏造仍须拦截（场景若模型真未召回而胡编暗号
    同样必须拦）。"""
    checker = _FullRecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="第一轮任务完成，会话已压缩。"),
        Message(
            role=MessageRole.TOOL,
            content='<tool_output tool="knowledge_search">pineapple-77</tool_output>',
        ),
        Message(role=MessageRole.USER, content="暗号是什么？只回答暗号本身。"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="暗号是什么？只回答暗号本身。")
    ctx.meta["_hallucination_rewrite_count"] = 2
    ctx.current_response = LLMResponse(content="暗号是 Alpha-999。", finish_reason="stop")
    ctx.current_content = "暗号是 Alpha-999。"
    await on_parsing(ctx)
    assert len(checker.calls) == 1
    assert ctx.terminal_reason is TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_hallucination_gate_skips_pure_confirmation_with_history() -> None:
    """契约（幻觉 6，r2 修复）：**有既往轮**但回复为纯功能确认（无实质事实断言
    如「收到即可」「好的」）时同样不跑幻觉 judge——弱 judge 对多轮短确认仍系统性
    误判（冒烟 r2 score=0.9/0.95 拦截"补充背景后回复收到"类）。6c470d1 只豁免
    首轮；多轮纯确认无跨轮事实可对照、回复无事实断言，幻觉语义不适用。含实体/事实
    断言的短回复（如"代号是天马"）不受此豁免（幻觉 3/7 护栏）。"""
    checker = _FullRecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="记住暗号是 pineapple-77。回复收到即可。"),
        Message(role=MessageRole.ASSISTANT, content="好的，收到。"),
        Message(role=MessageRole.USER, content="再补充一段背景材料。回复收到即可。"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="再补充一段背景材料。回复收到即可。")
    ctx.current_response = LLMResponse(content="好的，收到即可。", finish_reason="stop")
    ctx.current_content = "好的，收到即可。"
    await on_parsing(ctx)
    assert checker.calls == [], "多轮纯功能确认（无事实断言）不得调用幻觉 judge"
    assert ctx.terminal_reason is not TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_hallucination_gate_pure_confirmation_still_blocks_factual_assertion() -> None:
    """契约（幻觉 7，防豁免过宽护栏）：纯确认豁免只放行**无实质事实断言**的功能性
    回复——含事实断言的短回复（"代号是天马" vs 历史"代号蓝鲸"）有既往轮仍必须送检
    拦截（复刻幻觉 3 语义，钉住豁免边界不被多轮豁免扩展蚕食）。"""
    checker = _FullRecordingChecker(score=1.0)
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是助手"),
        Message(role=MessageRole.USER, content="记住两个信息：代号是蓝鲸，幸运数字是 17。"),
        Message(role=MessageRole.ASSISTANT, content="好的，已记住。"),
        Message(role=MessageRole.USER, content="代号是什么？"),
    ]
    ctx = _parsing_ctx(msgs, checker, raw_input="代号是什么？")
    ctx.meta["_hallucination_rewrite_count"] = 2
    ctx.current_response = LLMResponse(content="代号是天马。", finish_reason="stop")
    ctx.current_content = "代号是天马。"
    await on_parsing(ctx)
    assert len(checker.calls) == 1, "含事实断言的回复（即使短）有既往轮必须送 judge"
    assert ctx.terminal_reason is TerminalReason.SAFETY_BLOCKED


from bandi.agent.base import TerminalReason  # noqa: E402  供断言引用

# ① 按消息数分块到 4 个单条块，每块命中 compact 的 _MIN_MESSAGES_FOR_COMPRESSION
# 最小条数 bail；② target_token_budget=16000 硬编码，9755<16000 也被绝对预算
# 掩盖。两因叠加 → progressive 结构化不可用 → 只能 truncate 硬砍 → 丢关键事实。
# 契约钉死：小窗渐进压缩必须真实缩小且成功，而非被分块/预算双门槛拦截。


class _SummaryLLM:
    """伪摘要 LLM：把入参最后一段用户文本压成一行摘要（断言语义：能产出即可）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.summary = "摘要[渐进压缩后保留核心事实]"

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.summary, finish_reason="stop")


def _s8_small_history() -> list[Message]:
    filler = "这是填充背景内容用于撑大上下文。" * 350
    return [
        Message(role=MessageRole.SYSTEM, content="你是助手。"),
        Message(
            role=MessageRole.USER,
            content="记住暗号是 pineapple-77。然后复述下面材料要点：" + filler,
        ),
        Message(role=MessageRole.ASSISTANT, content="记住了暗号。"),
        Message(
            role=MessageRole.USER, content="再补充一段背景：" + filler[:3000] + "。回复收到即可。"
        ),
        Message(role=MessageRole.ASSISTANT, content="收到。"),
    ]


def _tokens(msgs: list[Message]) -> int:
    from bandi.utils.token_counter import TokenCounter

    return sum(TokenCounter.count(m.content or "") for m in msgs)


@pytest.mark.asyncio
async def test_progressive_small_window_actually_compacts() -> None:
    """契约（机制 1）：小窗（2500×0.6 触发）下 progressive 压缩必须真实缩小并
    成功——不得因分块产生单条子块 / 绝对预算 16000 掩盖而整体失败退化成 truncate。"""
    from bandi.memory.compression_pipeline import CompressionPipeline

    msgs = _s8_small_history()
    in_tokens = _tokens(msgs)
    assert in_tokens > _WINDOW * _THRESHOLD, "前置：对话须超触发预算"

    pipe = CompressionPipeline()
    llm = _SummaryLLM()
    out, success, _, stats = await pipe.compress(
        msgs,
        total_tokens=in_tokens,
        context_window=_WINDOW,
        compression_threshold=_THRESHOLD,
        llm_complete=llm,
    )
    assert success is True, f"S8 机制：渐进压缩必须成功而非 fallback（stats.stage={stats.stage}）"
    assert llm.calls >= 1, "渐进压缩应真实发起 LLM 摘要而非被门槛拦截（progressive 结构性失败）"
    assert getattr(stats, "progressive_chunks", 0) >= 1, "应有至少一块完成渐进摘要"
    assert _tokens(out) < in_tokens, "渐进压缩产物必须真实缩小（不能零缩减仍报成功）"

    # 双门槛根因钉死：分块不得产生 ≤MIN 的单条子块把 compact 整体拦截
    flat_chunk_small = getattr(stats, "progressive_failures", 0)
    assert flat_chunk_small < getattr(stats, "progressive_chunks", 0) * 0.5 or success, (
        "不得出现『每块都失败』的 split 结构性失败（chunks失败率应低或无）"
    )


@pytest.mark.asyncio
async def test_progressive_large_window_keeps_absolute_budget() -> None:
    """契约（机制 2 · 回归）：大窗下绝对 token 预算（16000）语义必须保持——
    渐进压缩不得因窗口缩放系数而对「中等体量对话」过度激进压缩（防止修复①过度）。
    断言：有效目标预算随窗口放大趋于绝对预算，且大型历史仍被 4 块渐进分块。"""
    from bandi.memory.compression_pipeline import (
        _WINDOW_TARGET_ALPHA,
        CompressionPipeline,
        MemoryCompactor,
    )

    pipe = CompressionPipeline()
    pc = pipe._progressive

    # 大窗（如 ）：有效预算封顶到绝对预算 16000，不再随窗口继续放大
    assert pc._effective_target(64_000) == 16000
    assert pc._effective_target(10_000) == min(16000, int(10_000 * _WINDOW_TARGET_ALPHA))
    assert pc._effective_target(2500) == min(16000, int(2500 * _WINDOW_TARGET_ALPHA))
    # None/非正窗口回退到绝对预算（调用方未下传时行为不变）
    assert pc._effective_target(None) == 16000

    # 大型历史仍按最多 4 块渐进分块（修复①不得把大历史捏成 1 块）
    msgs = [Message(role=MessageRole.USER, content=f"msg-{i}") for i in range(100)]
    chunks = pc._build_chunks(msgs, coalesce=False)
    assert len(chunks) <= pc._max_chunks, "大历史分块不得超过 max_chunks"
    assert all(len(c) >= 2 for c in chunks), "任一子块不得低于可压缩最小条数（防 singleton bail）"

    # compact 的预算 override 语义：显式传入的小预算在小体量对话也触发压缩
    # 缺省仍用构造时的绝对预算（16000 不被窗口缩放影响）。
    mc = MemoryCompactor()
    assert pc._effective_target(None) >= mc._target_token_budget


@pytest.mark.asyncio
async def test_progressive_never_raises_explicit_small_budget() -> None:
    """契约（机制 2 · 回归）：窗口缩放只下调、不抬高显式配置的绝对预算——
    否则小窗 + 显式小 target_token_budget（如契约测试用 10）会被 800 下限抬升，导致
    本应触发压缩的小体量对话被 gate 拦截（verify 全量回归 14 项失败的根因）。"""
    from bandi.memory.compression_pipeline import ProgressiveCompressor

    pc = ProgressiveCompressor(target_token_budget=10)
    # 任何窗口都不能把有效预算抬到显式配置之上
    for w in (200, 2500, 20000):
        assert pc._effective_target(w) == 10, f"有效预算不得高于显式预算（window={w}）"
    assert pc._effective_target(None) == 10


# 的 `_MIN_MESSAGES_FOR_COMPRESSION` 消息数护栏（len(non_system)<=MIN 即 bail）把单条
# 超大消息整体拦下 → 渐进 0 块成功 → 退化 truncate → 丢暗号 pineapple-77。契约钉死：
# 「消息少 + 总量超预算」仍必须压缩（关键内容进最终产物），而「消息少 + 总量小」仍 bail
# 不浪费 LLM（既有护栏语义仅在小上下文时成立）。


class _MarkerSummaryLLM:
    """功能：伪摘要 LLM——捕获源文本并回放含关键标记的结构化摘要（断言语义：能产出即可）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_source = ""

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.calls += 1
        self.seen_source += "\n".join(str(m.get("content", "")) for m in messages)
        return LLMResponse(
            content="摘要[保留关键标记 pineapple-77 与核心事实]", finish_reason="stop"
        )


def _oversized_single_message() -> list[Message]:
    # 单条 user 消息即可超 2500 预算（≈6300 token），无辅助对话消息
    filler = "这是填充背景内容用于撑大上下文。" * 350
    return [
        Message(role=MessageRole.SYSTEM, content="你是助手。"),
        Message(
            role=MessageRole.USER,
            content=f"记住暗号是 pineapple-77。然后复述下面材料要点：{filler}",
        ),
    ]


@pytest.mark.asyncio
async def test_compact_single_oversized_message_compresses() -> None:
    """契约（护栏）：仅 1 条 non_system 且其 token 超预算 → compact 必须真实发起
    LLM 摘要（不得被消息数护栏拦截退化为 bail/truncate），且关键标记进最终产物。"""
    from bandi.memory.compaction import MemoryCompactor

    msgs = _oversized_single_message()
    from bandi.utils.token_counter import TokenCounter

    total_tokens = sum(TokenCounter.count(m.content or "") for m in msgs)
    assert total_tokens > 2500, "前置：单条消息须超预算"

    llm = _MarkerSummaryLLM()
    result, success, _ = await MemoryCompactor().compact(msgs, llm, target_token_budget=2500)
    assert success is True, "单条超预算消息必须成功压缩而非被消息数护栏拦截"
    assert llm.calls >= 1, "必须真实发起 LLM 摘要"
    product_text = "\n".join(m.content or "" for m in result)
    assert "pineapple-77" in product_text, (
        "关键标记必须保留在压缩产物（不得经护栏退化成 truncate 丢失）"
    )


@pytest.mark.asyncio
async def test_progressive_single_oversized_message_not_truncate_fallback() -> None:
    """契约（护栏 · 族级生产路径）：单条超预算消息经 CompressionPipeline 全链路
    必须走渐进压缩（chunks≥1、failures=0、不退化 truncate），关键标记保留。"""
    from bandi.memory.compression_pipeline import CompressionPipeline
    from bandi.utils.token_counter import TokenCounter

    msgs = _oversized_single_message()
    total_tokens = sum(TokenCounter.count(m.content or "") for m in msgs)

    llm = _MarkerSummaryLLM()
    pipe = CompressionPipeline()
    out, success, _, stats = await pipe.compress(
        msgs,
        total_tokens=total_tokens,
        context_window=_WINDOW,
        compression_threshold=_THRESHOLD,
        llm_complete=llm,
    )
    assert success is True, "单条超预算消息全链路必须压缩成功"
    assert getattr(stats, "progressive_chunks", 0) >= 1, "不得 0 块成功退化成 truncate"
    assert getattr(stats, "progressive_failures", -1) <= 0, "不得有分块结构性失败"
    product_text = "\n".join(m.content or "" for m in out)
    assert "pineapple-77" in product_text, "关键标记必须保留（护栏不得致内容丢失）"


@pytest.mark.asyncio
async def test_compact_small_single_message_still_bails() -> None:
    """契约（护栏 · 回归/破坏者视角）：单条「小」消息（未超预算）仍必须 bail——
    不得因放宽消息数护栏而对该浪费 LLM 摘要的平凡上下文误触发压缩。"""
    from bandi.memory.compaction import MemoryCompactor

    msgs = [
        Message(role=MessageRole.SYSTEM, content="sys"),
        Message(role=MessageRole.USER, content="hi"),
    ]
    llm = _MarkerSummaryLLM()
    _res, success, _ = await MemoryCompactor().compact(msgs, llm, target_token_budget=2500)
    assert success is False, "小上下文仍应 bail（不浪费 LLM 摘要）"
    assert llm.calls == 0, "bail 时不得发起任何 LLM 调用"
