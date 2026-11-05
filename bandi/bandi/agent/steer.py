"""Codex/LeAgent 式 mid-turn 交互：steer 消息在工具批边界被 drain 并作为新的
USER 消息追加进历史（append-only，不改写已发上下文）。

保持 API 兼容；本队列为多条 FIFO 扩展——连续多次转向不再互相覆盖。
"""

from __future__ import annotations

import threading
from collections import deque


class SessionSteerRegistry:
    """Per-session steer 队列（进程内单例）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, deque[str]] = {}

    def push(self, session_id: str | None, content: str, *, queue_only: bool = False) -> None:
        """功能：转向文本入口——默认活跃会话走实时注入、否则入队；queue_only=True
        跳过实时注入直接入队（loop 退出回退专用：ctx 正在退出，实时注入只会把
        文本写进死 ctx；不复用「标记终态」规避，终态语义保持原值）。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("steer content must be non-empty")
        if not session_id:
            raise ValueError("steer requires a session_id")
        if not queue_only:
            # 无活跃 ctx（会话未运行/已结束）才回退队列，保持既有语义。
            from bandi.core.agent_runner import inject_user_input

            if inject_user_input(str(session_id), content):
                return
        with self._lock:
            self._queues.setdefault(str(session_id), deque()).append(content)

    def drain(self, session_id: str | None) -> list[str]:
        """弹出该会话全部待处理 steer 消息（FIFO）；无队列/无 id 返回空。"""
        if not session_id:
            return []
        with self._lock:
            dq = self._queues.get(str(session_id))
            if not dq:
                return []
            out = list(dq)
            dq.clear()
            self._queues.pop(str(session_id), None)
        return out

    def has_pending(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return bool(self._queues.get(str(session_id)))


_default_registry = SessionSteerRegistry()


def get_default_registry() -> SessionSteerRegistry:
    """进程级默认 registry——API push 入口与 states 批边界 drain 共用；
    测试可经 ctx.meta["steer_registry"] 覆盖打桩。"""
    return _default_registry


__all__ = ["SessionSteerRegistry", "get_default_registry"]
