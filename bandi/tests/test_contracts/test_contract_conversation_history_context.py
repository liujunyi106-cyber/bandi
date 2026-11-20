"""功能：声明依赖上下文的工具必须同时暴露 `needs_context = True`。

真机缺陷（Agent 反复重试同一工具，界面长期「正在执行…」）：

    [WARN] tool conversation_history failure count: 1

`ConversationHistoryTool.execute` 从 `context.extra["session_id"]` 取当前会话，
否则返回 `{"ok": False, "error": "session_id is required"}`。
而 `executor.py` 只在 `tool.needs_context` 为真时才注入该字段：

    if getattr(tool, "needs_context", False):
        extra["session_id"] = ...

该类漏声明 `needs_context`（基类默认 False）⇒ 工具**恒失败**，
Agent 只能反复重试，熔断阈值又因记账问题迟迟不触发。

契约钉两件事：
1. `ConversationHistoryTool.needs_context` 必须为真；
2. 有 `session_id` 时正常返回，无 `session_id` 时 fail-closed（不得静默返回空历史）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.util.conversation_history import ConversationHistoryTool


class _Ctx:
    """功能：最小 context 替身——只提供 extra 字典。"""

    def __init__(self, session_id: str) -> None:
        self.extra = {"session_id": session_id}


def test_needs_context_declared() -> None:
    """功能：缺这一行工具恒失败（executor 不会注入 session_id）。"""
    assert ConversationHistoryTool.needs_context is True, (
        "conversation_history 依赖 context.extra['session_id']，"
        "必须声明 needs_context=True，否则 executor 不注入、工具恒报 session_id is required"
    )


def test_base_default_is_false() -> None:
    """功能：基类默认仍为 False（防把 needs_context 改成全局默认）。"""
    assert BaseTool.needs_context is False


def test_returns_history_with_injected_session(tmp_path: Path) -> None:
    """功能：注入 session_id 后必须成功（不抛、不返回错误码）。

    必须隔离真实库：直接用默认 `~/.bandi/data/bandi.db` 会在并行/只读环境下
    `sqlite3.OperationalError: unable to open database file`（FM-62：默认路径组件必须 tmp 隔离）。
    """
    tool = ConversationHistoryTool(session_db_path=str(tmp_path / "s.db"))
    result = asyncio.run(tool.execute({"operation": "get", "limit": 3}, _Ctx("sess-contract")))
    assert result.get("ok") is True, f"注入 session 后仍失败：{result}"
    assert result.get("session_id") == "sess-contract"
    assert isinstance(result.get("messages"), list)


def test_fail_closed_without_session(tmp_path: Path) -> None:
    """功能：无 session_id 必须显式失败（不得静默返回空历史让人误判为“无记录”）。"""
    tool = ConversationHistoryTool(session_db_path=str(tmp_path / "s.db"))
    result = asyncio.run(tool.execute({"operation": "get"}))
    assert result.get("ok") is False
    assert "session_id" in str(result.get("error", ""))


def test_permission_context_type_accepts_extra() -> None:
    """功能：ToolPermissionContext 必须有 extra 字段（注入通道存在）。"""
    ctx = ToolPermissionContext(user_id="u", role="user")
    assert hasattr(ctx, "extra")
