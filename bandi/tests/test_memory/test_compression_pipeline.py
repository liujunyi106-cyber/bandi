from pathlib import Path

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compression_pipeline import (
    AttachmentTracker,
    AutoCompactScheduler,
    CompressionPipeline,
    FailureCircuitBreaker,
    MicroCompressor,
    ProgressiveCompressor,
    strip_images_from_messages,
)


async def _mock_complete(messages):
    return LLMResponse(
        content="这是渐进摘要",
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
        finish_reason="stop",
    )


class TestMicroCompressor:
    def test_deduplicate_consecutive_tool_outputs(self) -> None:
        """仅同 tool_call_id 的重复 TOOL 输出可去重（重试场景），不同 id 必须保留。"""
        msgs = [
            Message(role=MessageRole.USER, content="hello"),
            Message(role=MessageRole.TOOL, content="result_a", tool_call_id="1"),
            Message(role=MessageRole.TOOL, content="result_a", tool_call_id="1"),
            Message(role=MessageRole.TOOL, content="result_b", tool_call_id="3"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert stats.micro_deduplicated == 1
        assert len(result) == 3
        assert result[1].content == "result_a"

    def test_deduplicate_keeps_different_tool_call_ids(self) -> None:
        """回归：不同 tool_call_id 的相同输出必须保留（删除会产生孤儿 tool result）。"""
        msgs = [
            Message(role=MessageRole.USER, content="hello"),
            Message(role=MessageRole.TOOL, content="result_a", tool_call_id="1"),
            Message(role=MessageRole.TOOL, content="result_a", tool_call_id="2"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert stats.micro_deduplicated == 0
        assert len(result) == 3, "不同 tool_call_id 的 TOOL 消息不得去重"

    def test_merge_short_messages_keeps_tool_calls(self) -> None:
        """带 tool_calls 的短 assistant 消息不得被合并（合并会丢失 tool_calls 造成孤儿结果）。"""
        from bandi.core.message import ToolCall

        tool_call = ToolCall(id="tc1", name="read", arguments={"file_path": "x.py"})
        msgs = [
            Message(role=MessageRole.USER, content="hi there"),
            Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tool_call]),
            Message(role=MessageRole.TOOL, content="file content", tool_call_id="tc1"),
            Message(role=MessageRole.USER, content="ok fine"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert any(m.tool_calls for m in result), "tool_calls 必须保留"
        assert sum(1 for m in result if m.role == MessageRole.ASSISTANT and m.tool_calls) == 1, (
            "带 tool_calls 的消息不得被合并掉"
        )

    def test_trim_verbose_tool_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path))
        long_content = "x" * 800
        msgs = [
            Message(role=MessageRole.TOOL, content=long_content, tool_call_id="1"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert stats.micro_trimmed == 1
        assert "[已截断" in result[0].content
        assert "[full output persisted:" in result[0].content
        persisted = result[0].content.split("[full output persisted: ", 1)[1].split("]", 1)[0]
        assert Path(persisted).read_text(encoding="utf-8") == long_content

    def test_merge_short_messages(self) -> None:
        msgs = [
            Message(role=MessageRole.USER, content="hi"),
            Message(role=MessageRole.USER, content="how are you"),
            Message(role=MessageRole.ASSISTANT, content="fine thanks"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert stats.micro_merged > 0
        assert len(result) < 3

    def test_remove_empty_messages(self) -> None:
        msgs = [
            Message(
                role=MessageRole.USER,
                content="This is a long enough user message that definitely exceeds the short message threshold of 80 characters for merging purposes.",
            ),
            Message(role=MessageRole.USER, content=""),
            Message(role=MessageRole.ASSISTANT, content=None),
            Message(
                role=MessageRole.USER,
                content="Another sufficiently long message here to ensure it does not get merged with the first one during the compression process.",
            ),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert len(result) == 2

    def test_no_compression_for_small_input(self) -> None:
        msgs = [
            Message(role=MessageRole.USER, content="hello"),
        ]
        comp = MicroCompressor()
        result, stats = comp.compress(msgs)
        assert len(result) == 1
        assert stats.micro_deduplicated == 0
        assert stats.micro_trimmed == 0

    def test_stats_tracking(self) -> None:
        msgs = [
            Message(role=MessageRole.TOOL, content="a" * 800, tool_call_id="1"),
            Message(role=MessageRole.TOOL, content="a" * 800, tool_call_id="2"),
        ]
        comp = MicroCompressor()
        _, stats = comp.compress(msgs)
        assert stats.stage == "micro"
        assert stats.input_messages == 2
        assert stats.output_chars > 0


class TestAutoCompactScheduler:
    def test_strategy_none_below_threshold(self) -> None:
        sched = AutoCompactScheduler()
        strategy = sched.select_strategy(
            total_tokens=1000, context_window=100000, compression_threshold=0.85
        )
        assert strategy == "none"

    def test_strategy_micro_at_mid_threshold(self) -> None:
        sched = AutoCompactScheduler()
        strategy = sched.select_strategy(
            total_tokens=55000, context_window=100000, compression_threshold=0.5
        )
        assert strategy == "micro"

    def test_strategy_progressive_at_high_threshold(self) -> None:
        sched = AutoCompactScheduler()
        strategy = sched.select_strategy(
            total_tokens=90000, context_window=100000, compression_threshold=0.5
        )
        assert strategy == "progressive"

    def test_strategy_fast_growth_triggers_progressive(self) -> None:
        sched = AutoCompactScheduler()
        sched.record_tokens(50000)
        sched.record_tokens(60000)
        sched.record_tokens(70000)
        strategy = sched.select_strategy(
            total_tokens=70000, context_window=100000, compression_threshold=0.5
        )
        assert strategy in ("micro_progressive", "progressive")

    def test_record_tokens_history(self) -> None:
        sched = AutoCompactScheduler()
        sched.record_tokens(100)
        sched.record_tokens(200)
        sched.record_tokens(300)
        assert len(sched._token_history) == 3


class TestProgressiveCompressor:
    @pytest.mark.asyncio
    async def test_few_messages_returns_original(self) -> None:
        comp = ProgressiveCompressor()
        msgs = [Message(role=MessageRole.USER, content="hi")]
        result, stats = await comp.compress(msgs, _mock_complete)
        assert len(result) == 1
        assert stats.progressive_chunks == 0

    @pytest.mark.asyncio
    async def test_progressive_compression(self) -> None:
        comp = ProgressiveCompressor(max_chunks=2)
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="a" * 500),
            Message(role=MessageRole.ASSISTANT, content="b" * 500),
            Message(role=MessageRole.USER, content="c" * 500),
            Message(role=MessageRole.ASSISTANT, content="d" * 500),
            Message(role=MessageRole.USER, content="e" * 500),
        ]
        result, stats = await comp.compress(msgs, _mock_complete)
        assert stats.progressive_chunks >= 0
        assert len(result) <= len(msgs)

    @pytest.mark.asyncio
    async def test_progressive_stats(self) -> None:
        comp = ProgressiveCompressor(max_chunks=2)
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="hello world"),
            Message(role=MessageRole.ASSISTANT, content="fine thanks"),
            Message(role=MessageRole.USER, content="what is up"),
        ]
        _, stats = await comp.compress(msgs, _mock_complete)
        assert stats.stage == "progressive"
        assert stats.input_messages == 4

    @pytest.mark.asyncio
    async def test_keep_ratio_full_keeps_all_tail(self) -> None:
        comp = ProgressiveCompressor(max_chunks=2, keep_ratio=1.0)
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="a" * 500),
            Message(role=MessageRole.ASSISTANT, content="b" * 500),
        ]
        result, _ = await comp.compress(msgs, _mock_complete)
        non_system = [m for m in result if m.role != MessageRole.SYSTEM]
        assert len(non_system) == 2

    @pytest.mark.asyncio
    async def test_keep_ratio_small_keeps_at_least_one(self) -> None:
        comp = ProgressiveCompressor(max_chunks=2, keep_ratio=0.0)
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="a" * 500),
            Message(role=MessageRole.ASSISTANT, content="b" * 500),
            Message(role=MessageRole.USER, content="c" * 500),
        ]
        result, _ = await comp.compress(msgs, _mock_complete)
        non_system = [m for m in result if m.role != MessageRole.SYSTEM]
        assert len(non_system) >= 1


class TestCompressionPipeline:
    @pytest.mark.asyncio
    async def test_pipeline_none_strategy(self) -> None:
        pipeline = CompressionPipeline()
        msgs = [Message(role=MessageRole.USER, content="hi")]
        result, success, inc, stats = await pipeline.compress(
            msgs,
            total_tokens=100,
            context_window=100000,
            compression_threshold=0.85,
            llm_complete=_mock_complete,
        )
        assert success is False
        assert inc == 0
        assert stats.stage == "none"

    @pytest.mark.asyncio
    async def test_pipeline_micro_strategy(self) -> None:
        pipeline = CompressionPipeline()
        msgs = [
            Message(role=MessageRole.USER, content="hello"),
            Message(role=MessageRole.TOOL, content="a" * 800, tool_call_id="1"),
            Message(role=MessageRole.TOOL, content="a" * 800, tool_call_id="2"),
        ]
        result, success, inc, stats = await pipeline.compress(
            msgs,
            total_tokens=100000,
            context_window=100000,
            compression_threshold=0.5,
            llm_complete=_mock_complete,
        )
        assert success is True
        assert stats.stage in ("micro", "micro_progressive", "progressive")

    @pytest.mark.asyncio
    async def test_pipeline_truncate_fallback(self) -> None:
        msgs = [Message(role=MessageRole.USER, content="test")]
        result = CompressionPipeline.truncate(msgs)
        assert len(result) == 1

    def test_truncate_preserves_system(self) -> None:
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="a"),
            Message(role=MessageRole.ASSISTANT, content="b"),
        ]
        result = CompressionPipeline.truncate(msgs, min_keep=2)
        assert result[0].role == MessageRole.SYSTEM

    @pytest.mark.asyncio
    async def test_pipeline_reports_failure_when_progressive_unreduced(self) -> None:
        """progressive 压缩失败（LLM 摘要抛错）且未缩小预算时必须返回 success=False
        不得误报成功导致状态机误终止（MAX_TURNS）。"""

        async def _boom_complete(messages):
            raise RuntimeError("llm down")

        # 大上下文触发 micro→progressive 且 progressive 失败（内容各不相同避免去重；
        # context_window 取小值使字符预算低于输入，确保压缩确实必要）
        pipeline = CompressionPipeline()
        msgs = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            *[Message(role=MessageRole.USER, content=f"user-{i}-" + "x" * 300) for i in range(20)],
        ]
        result, success, inc, stats = await pipeline.compress(
            msgs,
            total_tokens=100000,
            context_window=1000,
            compression_threshold=0.5,
            llm_complete=_boom_complete,
        )
        assert success is False, (
            "progressive 全部失败且未缩小必须报告失败（否则状态机误 terminated）"
        )
        assert stats.progressive_failures > 0


class TestFailureCircuitBreaker:
    def test_starts_closed(self):
        cb = FailureCircuitBreaker(max_failures=3)
        assert not cb.is_tripped

    def test_trips_after_max_failures(self):
        cb = FailureCircuitBreaker(max_failures=2)
        cb.record_failure()
        assert not cb.is_tripped
        cb.record_failure()
        assert cb.is_tripped

    def test_reset_after_success(self):
        cb = FailureCircuitBreaker(max_failures=2, reset_after_success=True)
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0

    def test_manual_reset(self):
        cb = FailureCircuitBreaker(max_failures=1)
        cb.record_failure()
        assert cb.is_tripped
        cb.reset()
        assert not cb.is_tripped


class TestAttachmentTracker:
    def test_record_and_get(self):
        t = AttachmentTracker()
        t.record_stripped(0, "some image data")
        assert t.attachment_count == 1
        assert t.get_attachment(0) is not None

    def test_empty_summary(self):
        t = AttachmentTracker()
        assert t.summary() == ""

    def test_non_empty_summary(self):
        t = AttachmentTracker()
        t.record_stripped(1, "image: photo.png")
        assert "附件" in t.summary()


class TestStripImages:
    def test_strip_base64_image(self):
        msgs = [
            Message(role=MessageRole.USER, content="hello"),
            Message(role=MessageRole.TOOL, content="data:image/png;base64," + "A" * 200),
        ]
        result, tracker = strip_images_from_messages(msgs)
        assert len(result) == 2
        assert "[图片已压缩" in result[1].content
        assert tracker.attachment_count == 1

    def test_no_images_unchanged(self):
        msgs = [Message(role=MessageRole.USER, content="plain text")]
        result, tracker = strip_images_from_messages(msgs)
        assert len(result) == 1
        assert result[0].content == "plain text"
        assert tracker.attachment_count == 0


class TestProgressiveWithCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_after_failures(self):
        cb = FailureCircuitBreaker(max_failures=1)
        pc = ProgressiveCompressor(circuit_breaker=cb, max_chunks=1, target_token_budget=100)

        async def _llm_crash(messages):
            raise RuntimeError("LLM crash")

        msgs = [Message(role=MessageRole.USER, content="hello world " * 50)] * 20
        await pc.compress(msgs, _llm_crash)
        assert cb.is_tripped
        result, stats = await pc.compress(msgs, _llm_crash)
        assert stats.stage == "progressive_skipped_circuit_breaker"
