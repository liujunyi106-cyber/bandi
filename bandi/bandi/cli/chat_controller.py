# 功能：ChatTurnController — 异步消息队列
# 忙时消息入 deque → 自动消费，防止用户输入丢失

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QueuedChatInput:
    text: str
    mode: str = "execute"
    allowed_tools: list[str] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ChatTurnController:
    """消息队列控制器。Agent 忙时入队 → 当前任务完成 → 自动消费下一个。

    参照 参考实现 `opc/cli/app.py` ChatTurnController L2728-2943。"""

    def __init__(self) -> None:
        self._queue: deque[QueuedChatInput] = deque()
        self._active_task: asyncio.Task | None = None
        self._busy = False
        self._on_execute: Callable[[QueuedChatInput], Awaitable[None]] | None = None
        self._on_error: Callable[[QueuedChatInput, Exception], None] | None = None

    def set_on_error(self, on_error: Callable[[QueuedChatInput, Exception], None]) -> None:
        """注册主链路异常回调（界面反馈用——异常不再静默吞噬）。"""
        self._on_error = on_error

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    def set_executor(self, on_execute: Callable[[QueuedChatInput], Awaitable[None]]) -> None:
        """注册消息执行器（接收 QueuedChatInput 的 async callable）。"""
        self._on_execute = on_execute

    def submit(self, item: QueuedChatInput) -> bool:
        """提交消息。不忙时立即执行，忙时入队。返回 True 表示立即执行。"""
        if not self._busy:
            self._busy = True
            self._active_task = asyncio.create_task(self._run(item))
            return True
        self._queue.append(item)
        return False

    async def _run(self, item: QueuedChatInput) -> None:
        try:
            if self._on_execute:
                await self._on_execute(item)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 记录错误供界面反馈（executor 自身负责用户可见错误输出）
            logger.exception("chat turn failed: %s", e)
            if self._on_error is not None:
                try:
                    self._on_error(item, e)
                except Exception:
                    logger.exception("chat error callback failed")
        finally:
            self._busy = False
            self._active_task = None
            if self._queue:
                next_item = self._queue.popleft()
                self.submit(next_item)

    async def shutdown(self) -> None:
        task = self._active_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._queue.clear()
        self._busy = False
