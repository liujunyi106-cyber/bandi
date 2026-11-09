# 功能：讨论线程 REST/WS 接线契约
# + 经 handle_chat_message 的 @mention 建线程接线。断言最终产物（线程持久化 + 消息结构）。

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 装配隔离：Path.home→tmp + 重置全局 dispatcher（每次重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _dispatcher() -> Any:
    from bandi.api.app import _get_company_dispatcher

    return _get_company_dispatcher()


def _create_bound_thread() -> str:
    """真实 ThreadManager 建线程并写入两条消息，返回 thread_id。"""
    dispatcher = _dispatcher()
    mgr = dispatcher.threads
    assert mgr is not None, "dispatcher.threads 必须装配"
    thread = mgr.create_thread(parent_task_id="t_root", participants=["user", "tech"])
    mgr.add_message(thread.thread_id, "user", "@tech 请出方案", mentions=["tech"], message_id="w_1")
    mgr.add_message(thread.thread_id, "tech", "方案已出", message_id="w_2")
    return thread.thread_id


def test_threads_list_empty_then_populated() -> None:
    resp = client.get("/api/company/threads")
    assert resp.status_code == 200
    assert resp.json()["threads"] == []

    _create_bound_thread()

    resp = client.get("/api/company/threads")
    assert resp.status_code == 200
    threads = resp.json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert t["thread_id"]
    assert t["parent_task_id"] == "t_root"
    assert t["participants"] == ["user", "tech"]
    assert t["message_count"] == 2


def test_thread_detail_returns_messages_roundtrip() -> None:
    thread_id = _create_bound_thread()

    resp = client.get(f"/api/company/threads/{thread_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == thread_id
    assert data["parent_task_id"] == "t_root"
    assert data["participants"] == ["user", "tech"]
    msgs = data["messages"]
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "user"
    assert msgs[0]["content"] == "@tech 请出方案"
    assert msgs[0]["mentions"] == ["tech"]
    assert msgs[0]["message_id"] == "w_1"
    assert msgs[0]["created_at"]
    assert msgs[1]["sender"] == "tech"
    assert msgs[1]["message_id"] == "w_2"


def test_thread_detail_unknown_returns_404() -> None:
    resp = client.get("/api/company/threads/thread_missing")
    assert resp.status_code == 404


def test_thread_detail_returns_process_events() -> None:
    """过程快照必须随线程消息返回——单聊刷新后过程块可回看。"""
    thread_id = _create_bound_thread()
    dispatcher = _dispatcher()
    dispatcher.threads.add_message(
        thread_id,
        "tech",
        "带过程回复",
        message_id="w_3",
        process_events=[{"type": "thinking_delta", "data": {"text": "分析中"}}],
    )

    resp = client.get(f"/api/company/threads/{thread_id}")
    assert resp.status_code == 200
    msg = resp.json()["messages"][-1]
    assert msg["message_id"] == "w_3"
    assert msg["process_events"] == [{"type": "thinking_delta", "data": {"text": "分析中"}}]


def test_work_item_list_exposes_thread_id() -> None:
    """工作项列表必须暴露 thread_id——前端按线程区分「本会话在跑」与别的会话。"""
    dispatcher = _dispatcher()
    dispatcher.org_engine.register_role("tech", "Tech", "Technology")
    dispatcher.recruiter.register_seat("tech")

    from bandi.api.company_chat import handle_chat_message

    created = handle_chat_message(
        content="@tech 请出方案",
        sender_role="user",
        communication=dispatcher.communication,
        store=dispatcher.store,
        org_engine=dispatcher.org_engine,
        thread_manager=dispatcher.threads,
    )
    assert created
    thread_id = created[0].metadata["thread_id"]

    resp = client.get("/api/company/work-items", params={"role": "tech", "hide_chat": False})
    assert resp.status_code == 200
    items = [item for item in resp.json()["items"] if item["id"] == created[0].id]
    assert items and items[0]["thread_id"] == thread_id


def test_thread_message_persisted_after_new_dispatcher() -> None:
    """重构：新装配的 dispatcher（hydrate）必须从磁盘回读既有线程与消息。"""
    thread_id = _create_bound_thread()

    from bandi.api.app import _get_company_dispatcher  # 重取以触发重新装配

    with (
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
    ):
        _get_company_dispatcher()

    resp = client.get(f"/api/company/threads/{thread_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2
    assert data["messages"][1]["content"] == "方案已出"


def test_chat_mention_creates_thread_and_binds() -> None:
    """handle_chat_message 带 thread_manager：@mention 命中 → 建线程 + 卡 metadata 绑 thread_id。"""
    dispatcher = _dispatcher()
    dispatcher.org_engine.register_role("tech", "Tech", "Technology")
    dispatcher.recruiter.register_seat("tech")

    from bandi.api.company_chat import handle_chat_message

    created = handle_chat_message(
        content="@tech 请出方案",
        sender_role="user",
        communication=dispatcher.communication,
        store=dispatcher.store,
        org_engine=dispatcher.org_engine,
        thread_manager=dispatcher.threads,
    )

    assert created, "必须创建 work item"
    item = created[0]
    thread_id = item.metadata.get("thread_id")
    assert thread_id, "卡必须绑定 thread_id"
    thread = dispatcher.threads.get_thread(thread_id)
    assert thread is not None
    assert "tech" in thread.participants

    resp = client.get(f"/api/company/threads/{thread_id}")
    assert resp.status_code == 200
    assert any(m["content"] == "@tech 请出方案" for m in resp.json()["messages"])
