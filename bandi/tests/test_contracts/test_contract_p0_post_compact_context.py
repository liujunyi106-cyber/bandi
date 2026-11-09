# 功能：压缩后回注契约——技能/工具/计划必须回到上下文并可跨重启恢复
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bandi.agent.states import (
    POST_COMPACT_CONTEXT_HEADER,
    _inject_post_compact_context,
    _persist_compaction_boundary,
    _post_compact_context_text,
)
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        messages=[
            Message(role=MessageRole.SYSTEM, content="[上下文摘要] 旧摘要"),
            Message(role=MessageRole.USER, content="继续"),
        ],
        meta={
            "task_ledger": SimpleNamespace(
                items=[
                    {"status": "completed", "title": "检查依赖"},
                    {"status": "in_progress", "title": "跑回归测试"},
                ]
            ),
            "mode": "plan",
        },
        active_tools=[
            {"name": "bash", "description": "执行 shell 命令"},
            {"name": "mcp__deploy", "description": "触发部署"},
        ],
        turn_index=3,
    )


def test_post_compact_context_contains_tools_and_plan() -> None:
    ctx = _ctx()
    _inject_post_compact_context(ctx)
    text = next(
        m.content
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM
        and (m.content or "").startswith(POST_COMPACT_CONTEXT_HEADER)
    )
    assert "bash" in text
    assert "mcp__deploy" in text
    assert "跑回归测试" in text
    assert "已用技能" not in text, "F 模式不再回注技能正文（正文随工具结果）"


def test_post_compact_context_is_idempotent() -> None:
    ctx = _ctx()
    _inject_post_compact_context(ctx)
    _inject_post_compact_context(ctx)
    headers = [
        m
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM
        and (m.content or "").startswith(POST_COMPACT_CONTEXT_HEADER)
    ]
    assert len(headers) == 1


def test_post_compact_context_empty_when_nothing_to_reinject() -> None:
    ctx = SimpleNamespace(
        messages=[Message(role=MessageRole.USER, content="hi")],
        meta={},
        active_tools=None,
        turn_index=1,
    )
    assert _post_compact_context_text(ctx) == ""
    _inject_post_compact_context(ctx)
    assert len(ctx.messages) == 1


def test_post_compact_context_persisted_and_restored(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    ctx = _ctx()
    ctx.meta["session_store"] = store
    ctx.meta["session_id"] = sid
    _inject_post_compact_context(ctx)
    _persist_compaction_boundary(ctx, success=True, stage="progressive")

    entries = store.get_branch(sid)
    summary = next(e for e in entries if e.get("entry_type") == "summary")
    meta = json.loads(summary.get("metadata") or "{}")
    assert "bash" in meta.get("post_compact_context", "")

    from bandi.core.agent_runner import _session_restore

    restored = SimpleNamespace(messages=[])
    _session_restore(sid, store, restored)
    assert any((m.content or "").startswith(POST_COMPACT_CONTEXT_HEADER) for m in restored.messages)
    assert any("跑回归测试" in (m.content or "") for m in restored.messages)
