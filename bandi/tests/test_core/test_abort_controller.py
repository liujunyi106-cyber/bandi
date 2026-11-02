from __future__ import annotations

import asyncio
import signal

import pytest

from bandi.core.abort_controller import AbortController


class TestAbortController:
    def test_initial_state(self):
        ac = AbortController()
        assert ac.should_abort is False
        assert ac.reason == ""

    def test_abort_sets_event(self):
        ac = AbortController()
        ac.abort("test_reason")
        assert ac.should_abort is True
        assert ac.reason == "test_reason"

    def test_clear_resets(self):
        ac = AbortController()
        ac.abort("test")
        ac.clear()
        assert ac.should_abort is False
        assert ac.reason == ""

    def test_default_reason(self):
        ac = AbortController()
        ac.abort()
        assert ac.reason == "user_interrupt"

    @pytest.mark.asyncio
    async def test_wait_blocks_until_abort(self):
        ac = AbortController()
        results = []

        async def waiter():
            await ac.wait()
            results.append("done")

        asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        assert len(results) == 0
        ac.abort()
        await asyncio.sleep(0.01)
        assert len(results) == 1

    def test_signal_handler_sets_sigint_reason(self):
        ac = AbortController()
        ac.signal_handler(signal.SIGINT, None)
        assert ac.should_abort is True
        assert ac.reason == "sigint"

    def test_is_aborted_returns_tuple(self):
        """is_aborted 返回 (bool, str) 元组，避免 should_abort/reason 两步竞态"""
        ac = AbortController()
        assert ac.is_aborted() == (False, "")
        ac.abort("sigint")
        assert ac.is_aborted() == (True, "sigint")
        ac.clear()
        assert ac.is_aborted() == (False, "")
