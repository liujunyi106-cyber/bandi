import pytest

from bandi.agent.base import RecoveryAction
from bandi.agent.recovery_manager import DegradationState, ErrorSeverity, RecoveryManager


class TestErrorSeverity:
    def test_fatal_is_auto(self) -> None:
        assert ErrorSeverity.FATAL.value > 0

    def test_four_severities(self) -> None:
        names = {e.name for e in ErrorSeverity}
        assert names == {"FATAL", "RECOVERABLE", "DEGRADED"}


class TestDegradationState:
    def test_degrade_and_check(self) -> None:
        ds = DegradationState()
        ds.degrade("llm", "API 超时")
        assert ds.is_degraded("llm")
        assert not ds.is_degraded("tool")

    def test_recover(self) -> None:
        ds = DegradationState()
        ds.degrade("llm", "API 超时")
        ds.recover("llm")
        assert not ds.is_degraded("llm")

    def test_any_degraded(self) -> None:
        ds = DegradationState()
        assert not ds.any_degraded
        ds.degrade("tool", "文件权限拒绝")
        assert ds.any_degraded

    def test_snapshot(self) -> None:
        ds = DegradationState()
        ds.degrade("llm", "超时")
        ds.degrade("tool", "权限")
        snap = ds.snapshot()
        assert snap == {"llm": "超时", "tool": "权限"}


class TestRecoveryManager:
    def test_classify_fatal(self) -> None:
        rm = RecoveryManager()
        assert rm._classify(TypeError("bad"), "core") == ErrorSeverity.FATAL

    def test_classify_value_error_recoverable(self) -> None:
        rm = RecoveryManager()
        assert rm._classify(ValueError("bad input"), "core") == ErrorSeverity.RECOVERABLE

    def test_classify_recoverable(self) -> None:
        rm = RecoveryManager()
        assert rm._classify(TimeoutError("timeout"), "llm") == ErrorSeverity.RECOVERABLE

    def test_classify_degraded(self) -> None:
        rm = RecoveryManager()
        assert rm._classify(NotImplementedError("no impl"), "tool") == ErrorSeverity.DEGRADED

    @pytest.mark.asyncio
    async def test_handle_fatal_returns_terminate(self) -> None:
        rm = RecoveryManager()
        action = await rm.handle(TypeError("bad"), "core")
        assert action == RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_handle_value_error_returns_retry(self) -> None:
        rm = RecoveryManager()
        action = await rm.handle(ValueError("bad input"), "core")
        assert action == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_handle_recoverable_returns_retry(self) -> None:
        rm = RecoveryManager()
        action = await rm.handle(TimeoutError("timeout"), "llm")
        assert action == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_handle_degraded_marks_and_continues(self) -> None:
        rm = RecoveryManager()
        action = await rm.handle(NotImplementedError("no search"), "tool")
        assert action == RecoveryAction.CONTINUE_DEGRADED
        assert rm.degradation.is_degraded("tool")

    @pytest.mark.asyncio
    async def test_consecutive_errors_accumulate_per_layer(self) -> None:
        rm = RecoveryManager()
        for _ in range(3):
            await rm.handle(TimeoutError("timeout"), "llm")
        assert rm.consecutive_errors == {"llm": 3}

    @pytest.mark.asyncio
    async def test_consecutive_errors_decrement_on_turn_completed(self) -> None:
        """B5a 7.7：轮完成递减（非全清）——3 次错误过一轮剩 2；单次一过性错误过一轮清零。"""
        rm = RecoveryManager()
        for _ in range(3):
            await rm.handle(TimeoutError("timeout"), "llm")
        rm.on_turn_completed()
        assert rm.consecutive_errors == {"llm": 2}, "轮完成必须递减而非全清"

        rm2 = RecoveryManager()
        await rm2.handle(TimeoutError("timeout"), "llm")
        rm2.on_turn_completed()
        assert rm2.consecutive_errors == {}, "单次一过性错误过一轮必须清零"

    @pytest.mark.asyncio
    async def test_escalate_after_10_consecutive(self) -> None:
        rm = RecoveryManager()
        for _ in range(10):
            await rm.handle(TimeoutError("timeout"), "llm")
        action = await rm.handle(TimeoutError("timeout"), "llm")
        assert action == RecoveryAction.ESCALATE

    @pytest.mark.asyncio
    async def test_fatal_resets_consecutive(self) -> None:
        rm = RecoveryManager()
        await rm.handle(TimeoutError("timeout"), "llm")
        await rm.handle(TimeoutError("timeout"), "llm")
        await rm.handle(TypeError("fatal"), "core")
        assert rm.consecutive_errors == {"llm": 2}

    @pytest.mark.asyncio
    async def test_layer_isolation_no_cross_contamination(self) -> None:
        rm = RecoveryManager()
        for _ in range(3):
            await rm.handle(TimeoutError("timeout"), "llm")
        action = await rm.handle(TimeoutError("timeout"), "tool")
        assert action == RecoveryAction.RETRY
        assert rm.consecutive_errors == {"llm": 3, "tool": 1}

    @pytest.mark.asyncio
    async def test_emit_degradation_failure_does_not_raise_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """降级事件发送失败：不抛到调用方 + 必须留 warning 日志（阶段 G）。"""

        class BoomBus:
            async def emit(self, event: object) -> None:  # type: ignore[no-untyped-def]
                raise RuntimeError("bus closed")

        import logging

        rm = RecoveryManager(bus=BoomBus())  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="bandi.agent.recovery_manager"):
            action = await rm.handle(NotImplementedError("no search"), "tool")
        assert action == RecoveryAction.CONTINUE_DEGRADED
        assert any("degradation" in r.message.lower() for r in caplog.records), "吞异常必须留日志"
