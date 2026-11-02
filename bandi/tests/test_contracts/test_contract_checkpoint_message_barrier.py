# 功能：契约——屏障语义：checkpoint 写入前未落库消息必须先 flush（恢复一致性）
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.checkpoint import CheckpointStore
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.session.store import SessionStore


def _setup(tmp_path: Path) -> tuple[Any, Any, Any, Any, str]:
    ckpt = CheckpointStore(str(tmp_path / "ckpt.db"))
    sess = SessionStore(str(tmp_path / "sess.db"))
    sid = sess.create_session(mode="execute")
    loop = AgentLoop(checkpoint_store=ckpt, checkpoint_interval=1, session_store=sess)
    ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=EventBus())
    ctx.meta["session_id"] = sid
    ctx.turn_index = 1
    return ckpt, sess, loop, ctx, sid


@pytest.mark.asyncio
async def test_checkpoint_flushes_pending_messages(tmp_path: Path) -> None:
    """契约（屏障）：写 checkpoint 前必须把 ctx 中尚未落库的助手/工具消息
    补写 session store——checkpoint 的恢复点语义是「此刻全部消息已持久化」
    ，否则恢复时 messages 缺尾而 state
    标量超前（两库分叉窗口）。"""
    ckpt, sess, loop, ctx, sid = _setup(tmp_path)
    ctx.messages = [
        Message(role=MessageRole.USER, content="请读文件"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc-1", name="read", arguments={"path": "a.txt"})],
            turn_index=1,
        ),
        Message(role=MessageRole.TOOL, content="文件内容", tool_call_id="tc-1", turn_index=1),
    ]
    assert loop._persist_checkpoint_now(ctx, turns_taken=1) is True

    entries = sess.get_branch(sid)
    roles = [e["role"] for e in entries if e.get("entry_type") == "message"]
    assert "tool" in roles, "工具消息必须在 checkpoint 前落库（屏障）"
    assert "assistant" in roles, "助手消息必须在 checkpoint 前落库（屏障）"
    assert ckpt.list_by_session(sid), "checkpoint 必须同时落盘"
    ckpt.close()
    sess.close()


@pytest.mark.asyncio
async def test_checkpoint_flush_is_idempotent(tmp_path: Path) -> None:
    """破坏者（屏障幂等）：重复写 checkpoint 不得重复落库同一条消息——
    flush 每次全量扫描 ctx.messages，无幂等键会把历史刷成重复条目。"""
    ckpt, sess, loop, ctx, sid = _setup(tmp_path)
    ctx.messages = [
        Message(role=MessageRole.USER, content="问题"),
        Message(role=MessageRole.ASSISTANT, content="回答", turn_index=1),
    ]
    loop._persist_checkpoint_now(ctx, turns_taken=1)
    loop._persist_checkpoint_now(ctx, turns_taken=2)
    entries = [
        e
        for e in sess.get_branch(sid)
        if e.get("entry_type") == "message" and e.get("role") == "assistant"
    ]
    assert len(entries) == 1, f"同轮同内容助手消息不得因多次 flush 重复（实际 {len(entries)}）"
    ckpt.close()
    sess.close()
