"""契约：会话用量/上下文必须按 thread 口径聚合，而不是项目累计。

真机实证：DM 会话 252ca... 的 cost-detail 返回 654.3k（整个 group-test 项目），
而该 DM thread 实际只有 52.6k；上下文环则因为 chat session 没有快照显示 —。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

app_mod = sys.modules.get("bandi.api.app") or __import__("importlib").import_module("bandi.api.app")


def _fresh_store() -> Any:
    from bandi.api.app import _get_store

    api_module = sys.modules["bandi.api.app"]
    with api_module._store_lock:
        api_module._store = None
    return _get_store()


def _chat_session_with_thread(project_id: str, thread_id: str, title: str) -> str:
    store = _fresh_store()
    session_id = store.create_session(title=title)
    store.update_session(
        session_id,
        metadata=json.dumps(
            {"project_id": project_id, "thread_id": thread_id},
            ensure_ascii=False,
        ),
    )
    return session_id


def _enqueue_usage(session_id: str, tokens: int) -> None:
    from bandi.core.config import default_cost_db_path
    from bandi.observability.cost_store import CostRecord, get_shared_cost_store

    store = get_shared_cost_store(default_cost_db_path())
    store.enqueue(
        CostRecord(
            session_id=session_id,
            model="m1",
            input_tokens=tokens,
            output_tokens=7,
            breakdown={"input_tokens_total": tokens},
        )
    )
    store.flush()


def _snapshot(used: int) -> dict:
    return {
        "model": "m1",
        "context_window": 100_000,
        "used_tokens": used,
        "buckets": {
            "system": used,
            "tools": 0,
            "messages": 0,
            "connectors": 0,
            "skills": 0,
        },
        "updated_at": "2026-11-05T10:00:00",
    }


def test_cost_detail_scopes_to_session_thread() -> None:
    chat = _chat_session_with_thread("p1", "thread_a", "DM")
    _enqueue_usage("role-session::p1::thread_a::r1::e1", 100)
    _enqueue_usage("role-session::p1::thread_b::r1::e1", 300)

    result = asyncio.run(app_mod.session_cost_detail(chat))

    assert result["total_tokens"] == 107, result
    assert result["total_tokens"] != 407, "不得聚合整个项目"


def test_context_usage_scopes_to_session_thread() -> None:
    chat = _chat_session_with_thread("p1", "thread_a", "DM")
    store = _fresh_store()
    store.save_context_usage("role-session::p1::thread_a::r1::e1", _snapshot(50))
    store.save_context_usage("role-session::p1::thread_b::r1::e1", _snapshot(900))

    result = asyncio.run(app_mod.session_context_usage(chat))

    assert result["available"] is True
    assert result["used_tokens"] == 50
    assert result["context_window"] == 100_000


def test_new_thread_has_zero_cost_and_unavailable_context() -> None:
    chat = _chat_session_with_thread("p1", "thread_new", "新 DM")
    _enqueue_usage("role-session::p1::thread_old::r1::e1", 100)

    cost = asyncio.run(app_mod.session_cost_detail(chat))
    context = asyncio.run(app_mod.session_context_usage(chat))

    assert cost["total_tokens"] == 0
    assert context["available"] is False
    assert context["used_tokens"] == 0


def test_new_session_without_thread_does_not_aggregate_project() -> None:
    """未绑定线程的新会话必须从 0 开始，不得退化为项目累计。"""
    store = _fresh_store()
    chat = store.create_session(title="新会话")
    store.update_session(chat, metadata=json.dumps({"project_id": "p1"}, ensure_ascii=False))
    _enqueue_usage("role-session::p1::thread_a::r1::e1", 5000)

    cost = asyncio.run(app_mod.session_cost_detail(chat))
    context = asyncio.run(app_mod.session_context_usage(chat))

    assert cost["total_tokens"] == 0, "无 thread 会话不得聚合整个项目"
    assert context["available"] is False
    assert context["used_tokens"] == 0
