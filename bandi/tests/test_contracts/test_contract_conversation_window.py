# 功能：会话窗口（group/dm）契约——群聊会话绑定线程、删除级联、单聊删除重建、
# 员工上下文按窗口隔离、团队记忆不随会话删除。

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


def _ensure_role(role_id: str = "tech") -> None:
    dispatcher = _dispatcher()
    if dispatcher.org_engine.get_role(role_id) is None:
        dispatcher.org_engine.register_role(role_id, "Tech", "Technology")
    dispatcher.recruiter.register_seat(role_id)


def _chat(
    content: str,
    *,
    session_id: str = "",
    target_role: str | None = None,
) -> list[Any]:
    from bandi.api.company_chat import handle_chat_message

    dispatcher = _dispatcher()
    return handle_chat_message(
        content=content,
        sender_role="user",
        communication=dispatcher.communication,
        store=dispatcher.store,
        org_engine=dispatcher.org_engine,
        thread_manager=dispatcher.threads,
        target_role=target_role,
        project_id="",
        session_id=session_id,
    )


def test_group_session_binds_one_thread_and_marks_kind() -> None:
    _ensure_role()
    first = _chat("@tech 你好", session_id="sess-a")
    second = _chat("@tech 继续", session_id="sess-a")
    assert first
    assert second == [], "当前发言人未结束时不重复建卡"
    thread_id = first[0].metadata["thread_id"]
    round_ = _dispatcher().group_rounds.get_active("sess-a")
    assert round_ is not None
    assert round_.thread_id == thread_id
    thread = _dispatcher().threads.get_thread(thread_id)
    assert thread is not None
    assert thread.session_id == "sess-a"
    assert thread.kind == "group"


def test_group_sessions_are_isolated_windows() -> None:
    _ensure_role()
    a = _chat("@tech A", session_id="sess-a")
    b = _chat("@tech B", session_id="sess-b")
    assert a[0].metadata["thread_id"] != b[0].metadata["thread_id"]


def test_delete_session_cascades_thread_and_new_session_gets_new_thread() -> None:
    _ensure_role()
    from bandi.api.app import _bind_session_thread, _get_store

    store = _get_store()
    store.create_session(session_id="sess-del", title="群聊")
    created = _chat("@tech 旧窗口", session_id="sess-del")
    thread_id = created[0].metadata["thread_id"]
    _bind_session_thread("sess-del", thread_id)

    resp = client.delete("/api/sessions/sess-del")
    assert resp.status_code == 200
    assert resp.json()["thread_deleted"] is True
    assert client.get(f"/api/company/threads/{thread_id}").status_code == 404

    store.create_session(session_id="sess-new", title="新群聊")
    again = _chat("@tech 新窗口", session_id="sess-new")
    assert again[0].metadata["thread_id"] != thread_id


def test_dm_thread_delete_and_recreate() -> None:
    _ensure_role()
    first = _chat("单聊一", target_role="tech")
    thread_id = first[0].metadata["thread_id"]
    second = _chat("单聊二", target_role="tech")
    assert second[0].metadata["thread_id"] == thread_id

    resp = client.delete(f"/api/company/threads/{thread_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/company/threads/{thread_id}").status_code == 404

    third = _chat("单聊三", target_role="tech")
    assert third[0].metadata["thread_id"] != thread_id


def test_employee_context_uses_window_scope() -> None:
    from bandi.company.company_runtime import CompanyRuntime

    window = CompanyRuntime(
        project_id="p1",
        session_scope_id="task-1",
        role_id="tech",
        employee_id="tech",
        history_scope_id="thread_abc",
    ).member_session_id()
    other = CompanyRuntime(
        project_id="p1",
        session_scope_id="task-2",
        role_id="tech",
        employee_id="tech",
        history_scope_id="thread_xyz",
    ).member_session_id()
    legacy = CompanyRuntime(
        project_id="p1",
        session_scope_id="task-1",
        role_id="tech",
        employee_id="tech",
        history_scope_id="staff",
    ).member_session_id()
    assert "::thread_abc::tech::tech" in window
    assert "::thread_xyz::tech::tech" in other
    assert window != other
    assert "::staff::tech::tech" in legacy


def test_thread_list_exposes_window_fields() -> None:
    _ensure_role()
    _chat("@tech 列表", session_id="sess-list")
    body = client.get("/api/company/threads").json()
    thread = next(item for item in body["threads"] if item["session_id"] == "sess-list")
    assert thread["kind"] == "group"
