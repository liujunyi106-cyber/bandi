# 功能：自动压缩落库契约——压缩结果写 summary/boundary entry
# 重启恢复按最新 boundary 的 active_messages 投影，原始历史仍可审计。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


def _restore_ctx() -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    return ExecutionContext(messages=[], provider=None, config=AgentConfig(), bus=EventBus())


def _seed(store: SessionStore, sid: str, count: int) -> None:
    for i in range(count):
        store.append_message(sid, Message(role=MessageRole.USER, content=f"历史-{i}"))


def test_compaction_boundary_persists_and_projects_active_slice(tmp_path: Path) -> None:
    """契约：压缩落库后，恢复只回灌 active slice；原始 message 条目保留可审计。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="compact")
    _seed(store, sid, 10)

    # 快照只含非 SYSTEM 消息（摘要由 boundary 自身补回）
    active = [
        {"role": "user", "content": "最近任务"},
        {"role": "assistant", "content": "最近回复"},
    ]
    store.append_summary_entry(
        sid,
        3,
        "[上下文摘要] 前 8 条已折叠",
        metadata={"active_messages": active, "success": True, "stage": "progressive"},
    )

    # 原始历史仍在库里（可审计），boundary 独立成条；get_branch 默认已投影
    full = store.get_full_branch(sid)
    assert sum(1 for e in full if e.get("entry_type") == "message") == 10
    assert sum(1 for e in full if e.get("entry_type") == "summary") == 1
    assert [e.get("entry_type") for e in store.get_branch(sid)] == ["summary"]

    from bandi.core.agent_runner import _session_restore

    ctx = _restore_ctx()
    _session_restore(sid, store, ctx)

    assert [m.content for m in ctx.messages] == [
        "[上下文摘要] 前 8 条已折叠",
        "最近任务",
        "最近回复",
    ], "恢复必须按 active slice 投影，而不是读回全量旧消息"


def test_restore_without_boundary_keeps_full_history(tmp_path: Path) -> None:
    """回归护栏：无压缩 boundary 的普通会话保持全量恢复（零行为变化）。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="plain")
    _seed(store, sid, 6)

    from bandi.core.agent_runner import _session_restore

    ctx = _restore_ctx()
    _session_restore(sid, store, ctx)

    assert len(ctx.messages) == 6


def test_preserved_segment_projects_after_boundary(tmp_path: Path) -> None:
    """契约：压缩后 active slice 物理重挂到 boundary 之后——get_branch 只回切片，
    get_full_branch 仍能看到边界之前的原始历史。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="segment")
    _seed(store, sid, 4)

    store.append_summary_entry(
        sid,
        2,
        "[上下文摘要] 已折叠",
        metadata={"active_messages": [{"role": "system", "content": "[上下文摘要] 已折叠"}]},
    )
    store.append_preserved_segment(
        sid,
        [
            Message(role=MessageRole.USER, content="保留问题"),
            Message(role=MessageRole.ASSISTANT, content="保留回复"),
        ],
    )

    projected = store.get_branch(sid)
    assert [e.get("entry_type") for e in projected] == ["summary", "message", "message"]

    full = store.get_full_branch(sid)
    assert sum(1 for e in full if e.get("entry_type") == "message") == 6, "原始 4 条 + 重挂 2 条"

    from bandi.core.agent_runner import _session_restore

    ctx = _restore_ctx()
    _session_restore(sid, store, ctx)
    assert [m.content for m in ctx.messages] == ["[上下文摘要] 已折叠", "保留问题", "保留回复"]


def test_count_branch_excludes_pre_boundary_entries(tmp_path: Path) -> None:
    """契约：会话列表 message_count 只计活动切片，不被 preserved segment 虚高。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="count")
    _seed(store, sid, 4)
    assert store.count_branch(sid) == 4

    store.append_summary_entry(sid, 2, "[上下文摘要] 已折叠", metadata={})
    store.append_preserved_segment(sid, [Message(role=MessageRole.USER, content="保留")])

    assert store.count_branch(sid) == 1, "只计 boundary 之后的重挂消息（boundary 本身不算消息）"


def test_latest_boundary_wins_after_repeated_compaction(tmp_path: Path) -> None:
    """契约：多次压缩后按最新 boundary 投影（旧 boundary 不覆盖新切片）。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="compact-twice")
    _seed(store, sid, 8)

    store.append_summary_entry(
        sid,
        1,
        "[上下文摘要] 旧",
        metadata={"active_messages": [{"role": "user", "content": "旧任务"}]},
    )
    store.append_summary_entry(
        sid,
        2,
        "[上下文摘要] 新",
        metadata={"active_messages": [{"role": "user", "content": "新任务"}]},
    )

    from bandi.core.agent_runner import _session_restore

    ctx = _restore_ctx()
    _session_restore(sid, store, ctx)

    assert [m.content for m in ctx.messages] == ["[上下文摘要] 新", "新任务"]
