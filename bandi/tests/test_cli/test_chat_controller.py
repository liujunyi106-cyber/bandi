# 功能：ChatTurnController 单元测试 — submit / queue / shutdown / CancelledError

import asyncio

import pytest

from bandi.cli.chat_controller import ChatTurnController, QueuedChatInput


class TestChatTurnController:
    def test_initial_state(self) -> None:
        ctrl = ChatTurnController()
        assert not ctrl.is_busy
        assert ctrl.queue_length == 0

    @pytest.mark.asyncio
    async def test_submit_when_idle_executes_immediately(self) -> None:
        ctrl = ChatTurnController()
        executed = []

        async def on_execute(item: QueuedChatInput) -> None:
            executed.append(item.text)

        ctrl.set_executor(on_execute)
        ctrl.submit(QueuedChatInput(text="hello"))
        await asyncio.sleep(0.02)
        assert len(executed) == 1
        assert executed[0] == "hello"

    @pytest.mark.asyncio
    async def test_submit_when_busy_queues(self) -> None:
        ctrl = ChatTurnController()
        block = asyncio.Event()
        executed = []

        async def on_execute(item: QueuedChatInput) -> None:
            executed.append(item.text)
            if item.text == "first":
                await block.wait()

        ctrl.set_executor(on_execute)
        ctrl.submit(QueuedChatInput(text="first"))
        await asyncio.sleep(0.02)
        ctrl.submit(QueuedChatInput(text="second"))
        assert len(executed) == 1
        assert ctrl.queue_length == 1
        block.set()

    @pytest.mark.asyncio
    async def test_shutdown_no_active_task(self) -> None:
        ctrl = ChatTurnController()
        await ctrl.shutdown()
        assert not ctrl.is_busy
        assert ctrl.queue_length == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_task(self) -> None:
        ctrl = ChatTurnController()
        started = asyncio.Event()
        state = {"cancelled": False}

        async def slow_execute(item: QueuedChatInput) -> None:
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        ctrl.set_executor(slow_execute)
        ctrl.submit(QueuedChatInput(text="slow"))
        await started.wait()
        await ctrl.shutdown()
        assert state["cancelled"]
        assert not ctrl.is_busy
        assert ctrl.queue_length == 0

    @pytest.mark.asyncio
    async def test_shutdown_clears_queue(self) -> None:
        ctrl = ChatTurnController()
        block = asyncio.Event()

        async def blocking_execute(item: QueuedChatInput) -> None:
            await block.wait()

        ctrl.set_executor(blocking_execute)
        ctrl.submit(QueuedChatInput(text="busy"))
        await asyncio.sleep(0.02)
        ctrl.submit(QueuedChatInput(text="queued1"))
        ctrl.submit(QueuedChatInput(text="queued2"))
        assert ctrl.queue_length == 2
        await ctrl.shutdown()
        assert ctrl.queue_length == 0
        block.set()

    @pytest.mark.asyncio
    async def test_auto_consumes_queue_after_completion(self) -> None:
        ctrl = ChatTurnController()
        executed = []
        slow_event = asyncio.Event()

        async def execute_turn(item: QueuedChatInput) -> None:
            executed.append(item.text)
            if item.text == "first":
                await slow_event.wait()

        ctrl.set_executor(execute_turn)
        ctrl.submit(QueuedChatInput(text="first"))
        await asyncio.sleep(0.02)
        ctrl.submit(QueuedChatInput(text="second"))
        assert len(executed) == 1
        slow_event.set()
        await asyncio.sleep(0.05)
        assert len(executed) == 2
        assert executed == ["first", "second"]

    @pytest.mark.asyncio
    async def test_error_in_executor_unblocks_controller(self) -> None:
        ctrl = ChatTurnController()

        async def failing_execute(item: QueuedChatInput) -> None:
            raise RuntimeError("boom")

        ctrl.set_executor(failing_execute)
        ctrl.submit(QueuedChatInput(text="fail"))
        await asyncio.sleep(0.05)
        assert not ctrl.is_busy
        assert ctrl.queue_length == 0
