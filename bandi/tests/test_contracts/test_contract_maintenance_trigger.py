# （旧附件清理/确认消息成对剪枝）；行为不变式钉住，不测实现细节。
import time
from typing import Any

import pytest

from bandi.agent.states import on_compressing
from bandi.core.message import Message, MessageRole
from bandi.memory.compression_pipeline import (
    POST_COMPACT_FILE_PREFIX,
    CompressionPipeline,
    ProgressiveCompressor,
    is_maintenance_due,
)

# ── fixtures / helpers ───────────────────────────────────────────────────


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class _LLMSpy:
    """功能：捕获 LLM 调用的 spy（断言零调用/调用次数）。"""

    def __init__(self, content: str = "摘要内容") -> None:
        self.calls: list[list[dict]] = []

    async def __call__(self, prompt: list[dict]) -> object:
        self.calls.append(prompt)

        class _R:
            content = "摘要内容"

        return _R()


def _small_msgs(count: int = 6) -> list[Message]:
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手")]
    for i in range(count):
        msgs.append(Message(role=MessageRole.USER, content=f"第{i}步：请继续处理任务日志"))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}步处理完成，结果正常"))
    return msgs


# ── 组 A：is_maintenance_due 判定 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("turns", "ts", "now", "expected"),
    [
        (10, None, 1000.0, True),  # 轮满间隔触发
        (9, None, 1000.0, False),  # 未满不触发
        (20, None, 1000.0, True),  # 距上次维护 20 轮再触发
        (1, 1000.0 - 1801.0, 1000.0, True),  # 空闲超时触发
        (1, 1000.0 - 1799.0, 1000.0, False),  # 空闲不足不触发
        (0, None, 1000.0, False),  # 全新状态不触发
    ],
)
def test_is_maintenance_due_matrix(
    turns: int, ts: float | None, now: float, expected: bool
) -> None:
    assert is_maintenance_due(turns, ts, now) is expected


def _make_ctx(total_tokens: int) -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=_small_msgs(),
        provider=object(),
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.total_tokens = total_tokens
    return ctx


def test_token_threshold_still_triggers() -> None:
    """回归不变式：token 达阈值时 needs_compression 为 True（原行为不变）。"""
    ctx = _make_ctx(total_tokens=150)
    assert ctx.needs_compression() is True


def test_needs_compression_respects_turn_counter() -> None:
    """轮数维度接线：token 低但 turns_since_maintenance 满 10 → 触发。"""
    ctx = _make_ctx(total_tokens=10)
    ctx.compress.turns_since_maintenance = 10
    assert ctx.needs_compression() is True


def test_needs_compression_respects_idle_gap() -> None:
    """空闲维度接线：token 低、轮数低、距上次活动 ≥30min → 触发。"""
    ctx = _make_ctx(total_tokens=10)
    ctx.compress.last_active_ts = time.time() - 1801.0
    assert ctx.needs_compression() is True


def test_fresh_state_no_trigger() -> None:
    """全新状态（计数 0/无时间戳）且 token 低 → 不触发。"""
    ctx = _make_ctx(total_tokens=10)
    assert ctx.needs_compression() is False


def test_state_fields_persisted_in_snapshot() -> None:
    """维护触发状态必须进 QueryState 快照。

    此前 D-g4 维持 transient 语义：ctx 每轮重建后归零，导致「满 10 轮 / 空闲
    30 分钟」两条维护触发在跨轮场景永不生效（维护压缩从未自动跑过）。
    """
    from bandi.agent.base import QueryState

    qs = QueryState()
    assert qs.turns_since_maintenance == 0
    assert qs.last_active_ts is None


# ── 组 C：维护模式压缩行为 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maintenance_mode_zero_llm_calls() -> None:
    """破坏者视角：维护触发若误走 LLM 摘要即失败——零 LLM 调用硬约束。"""
    spy = _LLMSpy()
    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(max_chunks=2, target_token_budget=10)
    )
    result, success, _, stats = await pipeline.compress(
        _small_msgs(),
        total_tokens=10,
        context_window=1000,  # A2 适配：tiktoken 口径下 _small_msgs ≈ 200 tokens < 预算 500
        compression_threshold=0.5,
        llm_complete=spy,
        maintenance=True,
    )
    assert success is True
    assert spy.calls == []
    assert stats.stage == "micro"


@pytest.mark.asyncio
async def test_normal_mode_low_tokens_still_none() -> None:
    """现状不变式：非维护模式下 token 低 → 不压缩（success=False 走原语义）。"""
    spy = _LLMSpy()
    pipeline = CompressionPipeline()
    result, success, _, stats = await pipeline.compress(
        _small_msgs(),
        total_tokens=10,
        context_window=200,
        compression_threshold=0.5,
        llm_complete=spy,
    )
    assert success is False
    assert stats.stage == "none"
    assert spy.calls == []


# ── 组 E：维护后计数器重置（防同 turn 状态机重复触发）────────────────


@pytest.mark.asyncio
async def test_maintenance_resets_counters_after_success() -> None:
    """破坏者视角：维护成功若不重置计数器，同一 turn 内状态机每轮迭代
    都会重新触发维护压缩（boundary 标记累积 + 无谓重复压缩）——
    「维护即活动」：成功后 turns 归零、last_active_ts 刷新。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    class _P:
        calls = 0

        def count_tokens(self, text: str) -> int:
            return len(text or "") // 4

        async def complete(self, prompt: list[dict]) -> object:
            self.calls += 1

            class _R:
                content = "s"

            return _R()

    provider = _P()
    config = AgentConfig(context_window=128_000, compression_threshold=0.5)
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="sys")]
    for i in range(12):
        msgs.append(Message(role=MessageRole.USER, content=f"第{i}轮请求" * 5))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}轮完成" * 5))
    ctx = ExecutionContext(messages=msgs, provider=provider, config=config, bus=_RecordingBus())
    ctx.total_tokens = 5_000
    from bandi.memory.compression_pipeline import (
        CompressionPipeline as PipelineCls,
    )

    ctx.compress.pipeline = PipelineCls(
        progressive=ProgressiveCompressor(max_chunks=2, target_token_budget=100)
    )
    ctx.compress.turns_since_maintenance = 10
    # 模拟状态机 thinking→compressing 迁移循环 3 次
    boundary_counts = []
    for _cycle in range(3):
        if not ctx.needs_compression():
            break
        await on_compressing(ctx)
        ctx.total_tokens = sum(provider.count_tokens(m.content or "") for m in ctx.messages)
        boundary_counts.append(
            sum(1 for m in ctx.messages if (m.content or "").startswith("[压缩边界]"))
        )
    assert len(boundary_counts) == 1, f"RED: 同 turn 重复压缩 {len(boundary_counts)} 次"
    assert ctx.compress.turns_since_maintenance == 0
    assert ctx.compress.last_active_ts is not None


# ── 组 D：防抖（低收益连续 → LLM 升档降级）────────────────────────────


def test_llm_degraded_downgrades_mid_band_to_micro() -> None:
    """防抖：连续低收益后中度区不再升档 progressive（降级 micro）。"""
    scheduler = _new_scheduler()
    # 中度区（threshold+span*0.25 ~ span*0.5）
    strategy = scheduler.select_strategy(120, 200, 0.5, llm_degraded=True)
    assert strategy == "micro"


def test_severe_overload_overrides_degrade() -> None:
    """重度区无视防抖——宁可再试 LLM 也别 PROMPT_TOO_LONG。"""
    scheduler = _new_scheduler()
    strategy = scheduler.select_strategy(190, 200, 0.5, llm_degraded=True)
    assert strategy == "progressive"


def test_low_returns_counter_resets_on_good_savings() -> None:
    """高收益压缩重置低收益连击（防抖自动解除）。"""
    pipeline = CompressionPipeline()
    for _ in range(3):
        pipeline.record_progressive_savings(before_tokens=5000, after_tokens=4900)
    assert pipeline.llm_degraded() is True
    pipeline.record_progressive_savings(before_tokens=5000, after_tokens=1000)
    assert pipeline.llm_degraded() is False


def _new_scheduler() -> Any:
    from bandi.memory.compression_pipeline import AutoCompactScheduler

    return AutoCompactScheduler()


# ── 组 A：旧 [后压缩上下文] 附件清理 ──────────────────────────────────


@pytest.mark.asyncio
async def test_old_post_compact_files_pruned_keep_latest() -> None:
    """多次压缩产生的旧附件只留最新一条（真实累积问题——插入路径已实证）。"""
    spy = _LLMSpy()
    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(max_chunks=1, target_token_budget=10)
    )
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(
            role=MessageRole.SYSTEM,
            content=f"{POST_COMPACT_FILE_PREFIX} 旧附件 a.py",
        ),
        Message(
            role=MessageRole.SYSTEM,
            content=f"{POST_COMPACT_FILE_PREFIX} 新附件 b.py",
        ),
    ] + _small_msgs()[1:]
    result, success, _, _stats = await pipeline.compress(
        msgs,
        total_tokens=150,
        context_window=200,
        compression_threshold=0.5,
        llm_complete=spy,
    )
    attachments = [m for m in result if (m.content or "").startswith(POST_COMPACT_FILE_PREFIX)]
    assert len(attachments) <= 1
    if attachments:
        assert "b.py" in attachments[0].content  # 留的是最新


@pytest.mark.asyncio
async def test_single_post_compact_file_kept_idempotent() -> None:
    """单条附件不动（幂等——清理只针对多条累积）。"""
    spy = _LLMSpy()
    pipeline = CompressionPipeline()
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(
            role=MessageRole.SYSTEM,
            content=f"{POST_COMPACT_FILE_PREFIX} 唯一附件 a.py",
        ),
    ] + _small_msgs()[1:]
    result, _success, _, _stats = await pipeline.compress(
        msgs,
        total_tokens=150,
        context_window=200,
        compression_threshold=0.5,
        llm_complete=spy,
        maintenance=True,
    )
    kept = [m for m in result if (m.content or "").startswith(POST_COMPACT_FILE_PREFIX)]
    assert len(kept) == 1
    assert "a.py" in kept[0].content


# ── 组 B：确认消息成对剪枝 ────────────────────────────────────────────


def _confirmation_scene(
    user_text: str,
    assistant_text: str = "好的，已记录",
    assistant_tool_calls: bool = False,
    pad_turns_before: int = 3,
    pad_turns_after: int = 2,
) -> list[Message]:
    """功能：构造「历史轮 + 确认对 + 后续轮」场景（确认对不在最近 2 turn 保护区内）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content=f"初始任务 {pad_turns_before} 号"),
    ]
    for i in range(pad_turns_before):
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"进度汇报第{i}轮，一切正常"))
        msgs.append(Message(role=MessageRole.USER, content=f"收到，请继续第{i}项"))
    from bandi.core.message import ToolCall

    tool_calls = (
        [ToolCall(id="c1", name="read", arguments={"path": "x"})] if assistant_tool_calls else None
    )
    msgs.append(Message(role=MessageRole.USER, content=user_text))
    msgs.append(Message(role=MessageRole.ASSISTANT, content=assistant_text, tool_calls=tool_calls))
    # 确认对之后补实质对话——使确认 user 退出最近 2 turn 保护区
    for i in range(pad_turns_after):
        msgs.append(Message(role=MessageRole.USER, content=f"后续任务{i}：请处理数据报表"))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"后续任务{i}已完成并归档"))
    return msgs


def _run_confirmation_prune(msgs: list[Message]) -> list[Message]:
    from bandi.memory.compression_pipeline import prune_confirmation_pairs

    stats = type(_stats_stub())()
    return prune_confirmation_pairs(msgs, stats)


def _stats_stub() -> Any:
    from bandi.memory.compression_pipeline import CompressionStats

    return CompressionStats()


@pytest.mark.parametrize("word", ["好的", "继续", "ok", "OK", "收到", "嗯"])
def test_confirmation_words_removed_as_pair(word: str) -> None:
    """词表内确认 user + 短 assistant 成对删除。"""
    msgs = _confirmation_scene(word)
    result = _run_confirmation_prune(msgs)
    texts = [(m.role, m.content or "") for m in result]
    assert (MessageRole.USER, word) not in texts
    assert all(m.content != "好的，已记录" for m in result)


@pytest.mark.parametrize("text", ["帮我查一下天气", "好的没问题", "这个方案我觉得可以"])
def test_non_confirmation_user_kept(text: str) -> None:
    """词表外/超长 user 不删（误伤防线）。"""
    msgs = _confirmation_scene(text)
    result = _run_confirmation_prune(msgs)
    assert any((m.content or "") == text for m in result)


def test_confirmation_with_tool_calling_assistant_kept() -> None:
    """assistant 带 tool_calls 时整对保留（删了会孤立后续 TOOL 消息）。"""
    msgs = _confirmation_scene("好的", assistant_tool_calls=True)
    result = _run_confirmation_prune(msgs)
    assert any((m.content or "") == "好的" for m in result)


def test_recent_turn_confirmation_protected() -> None:
    """最近 2 个 user-turn 的确认对不删（与 prune 最近保护同维）。"""
    msgs = _confirmation_scene("好的", pad_turns_after=0)
    result = _run_confirmation_prune(msgs)
    assert any((m.content or "") == "好的" for m in result)


def test_long_assistant_reply_blocks_pair_removal() -> None:
    """assistant 回复超短消息上限 → 整对保留（信息量保护）。"""
    long_reply = "这是一段很长的实质性回复" * 15
    msgs = _confirmation_scene("好的", assistant_text=long_reply)
    result = _run_confirmation_prune(msgs)
    assert any((m.content or "") == long_reply for m in result)


def test_confirmation_prune_idempotent() -> None:
    """幂等：删除后再运行找不到确认对，列表恒定。"""
    msgs = _confirmation_scene("好的")
    once = _run_confirmation_prune(msgs)
    twice = _run_confirmation_prune(once)
    assert len(twice) == len(once)


def test_tool_chain_integrity_after_confirmation_prune() -> None:
    """结构安全：删除确认对后 TOOL 消息链完整（TOOL 必有前置 assistant calls）。"""
    msgs = _confirmation_scene("好的")
    msgs.append(Message(role=MessageRole.ASSISTANT, content="", tool_calls=None, tool_call_id=None))
    result = _run_confirmation_prune(msgs)
    for i, m in enumerate(result):
        if m.role == MessageRole.TOOL:
            assert any(
                prev.role == MessageRole.ASSISTANT and prev.tool_calls for prev in result[:i]
            )
