# 功能：取消控制器（信号驱动中止 + 等待中止状态）
from __future__ import annotations

import asyncio
from typing import Any


class AbortController:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str = ""

    @property
    def should_abort(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def abort(self, reason: str = "user_interrupt") -> None:
        self._reason = reason
        self._event.set()

    def clear(self) -> None:
        self._event.clear()
        self._reason = ""

    async def wait(self) -> None:
        await self._event.wait()

    def signal_handler(self, signum: int, frame: Any) -> None:
        self.abort("sigint")

    def is_aborted(self) -> tuple[bool, str]:
        """功能：返回 (should_abort, reason) 元组，避免两步读取竞态"""
        return self._event.is_set(), self._reason
