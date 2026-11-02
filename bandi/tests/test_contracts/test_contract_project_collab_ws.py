# 功能：契约——项目协作后端②（per-project 通信/线程路由 + 单聊 target_role 定向
# + WS 帧 project_id + 断线补发含 project）。
# 钉行为不钉实现：项目消息落盘隔离（p-alpha 可见 / company 不可见）/ 广播帧带 project_id /
# 单聊定向建 QUEUED 卡 + DM 落目标 inbox + 线程 participants=[user,target] + 连续归并 /
# 单聊元数据落盘（thread_id + target_role 白名单） / 破坏者：未注册 target / 项目不存在 /
# 非法 project_id 一律 error 帧不裸崩且零副作用 / resume 补发帧带 project_id。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.company.communication import CommunicationManager, list_inbox
from bandi.company.discussion_thread import ThreadManager
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 装配 + per-project 工厂隔离：Path.home→tmp + 全局单例/缓存重置。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._project_comms", {}),
        patch("bandi.api.app._project_threads", {}),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _receive_until(ws: object, wanted: str, tries: int = 8) -> dict:
    for _ in range(tries):
        frame = ws.receive_json()  # type: ignore[attr-defined]
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"未收到 {wanted} 帧")


def _create_project(project_id: str = "p-alpha", name: str = "选品项目") -> None:
    resp = client.post("/api/company/projects", json={"project_id": project_id, "name": name})
    assert resp.status_code == 200, resp.text


def _mk_org(*role_ids: str) -> OrgEngine:
    org = OrgEngine()
    for role_id in role_ids:
        org.register_role(role_id, role_id)
    return org


def _mk_comms(tmp_path: Path, org: OrgEngine | None = None) -> CommunicationManager:
    return CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )


class TestDmTargetRole:
    """handler 级：单聊定向。"""

    def test_dm_builds_queued_card_bound_to_dm_thread(self, tmp_path: Path) -> None:
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
        items = handle_chat_message(
            content="请处理这个",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            target_role="worker-1",
        )
        assert len(items) == 1, f"单聊必须建一张卡，实际 {items}"
        item = items[0]
        assert item.assignee_role == "worker-1"
        assert item.phase == Phase.QUEUED
        assert item.metadata.get("source") == "chat_dm"
        thread_id = item.metadata.get("thread_id")
        assert thread_id, "单聊卡必须绑定线程"
        thread = thread_mgr.get_thread(thread_id)
        assert thread is not None
        assert sorted(thread.participants) == ["user", "worker-1"], (
            f"单聊线程参与者必须为 [user, target]，实际 {thread.participants}"
        )

    def test_dm_consecutive_messages_merge_one_thread(self, tmp_path: Path) -> None:
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
        first = handle_chat_message(
            content="第一句",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            target_role="worker-1",
        )
        second = handle_chat_message(
            content="第二句",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            target_role="worker-1",
        )
        assert first[0].metadata["thread_id"] == second[0].metadata["thread_id"], (
            "连续单聊必须归并同一线程"
        )
        thread = thread_mgr.get_thread(first[0].metadata["thread_id"])
        assert thread is not None and len(thread.messages) == 2

    def test_dm_delivers_to_target_inbox_with_metadata(self, tmp_path: Path) -> None:
        """DM 落盘到目标 inbox（非 board）；thread_id/target_role 元数据不被白名单过滤。"""
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
        handle_chat_message(
            content="单聊落盘",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            target_role="worker-1",
        )
        target_inbox = list_inbox(str(tmp_path / "ws"), "p1", "s1", "worker-1")
        assert len(target_inbox) == 1, "单聊消息必须落目标 inbox"
        meta = target_inbox[0].metadata
        assert meta.get("kind") == "chat"
        assert meta.get("target_role") == "worker-1", f"target_role 不得被过滤，实际 {meta}"
        assert meta.get("thread_id", "").startswith("thread_"), f"thread_id 不得被过滤，实际 {meta}"
        assert list_inbox(str(tmp_path / "ws"), "p1", "s1", "board") == [], (
            "单聊不得落 board 群聊箱"
        )

    def test_dm_target_display_name_fallback(self, tmp_path: Path) -> None:
        """target_role 支持显示名唯一匹配（第二通道），归一为注册 id。"""
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = OrgEngine()
        org.register_role("sel-lead", "选品负责人")
        comm = _mk_comms(tmp_path, org)
        items = handle_chat_message(
            content="你好",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            target_role="选品负责人",
        )
        assert len(items) == 1
        assert items[0].assignee_role == "sel-lead"

    def test_dm_unregistered_target_fail_closed(self, tmp_path: Path) -> None:
        """破坏者：未注册 target_role → 显式拒绝，零副作用（不建卡不落盘）。"""
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        with pytest.raises(ValueError):
            handle_chat_message(
                content="幽灵目标",
                sender_role="user",
                communication=comm,
                store=store,
                org_engine=org,
                target_role="ghost-role",
            )
        assert store.list_all() == []
        assert list_inbox(str(tmp_path / "ws"), "p1", "s1", "ghost-role") == []

    def test_dm_ignores_mention_syntax(self, tmp_path: Path) -> None:
        """单聊不解析 @（target 优先）——内容含其他 @ 也不得给他人建卡。"""
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1", "worker-2")
        comm = _mk_comms(tmp_path, org)
        items = handle_chat_message(
            content="请处理 @worker-2 的事情",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            target_role="worker-1",
        )
        assert len(items) == 1
        assert items[0].assignee_role == "worker-1"
        assert store.list_all() and len(store.list_all()) == 1

    def test_card_metadata_carries_project_id(self, tmp_path: Path) -> None:
        """卡带 project_id（产出回写按此路由项目线程）。"""
        from bandi.api.company_chat import handle_chat_message

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        items = handle_chat_message(
            content="项目派活",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            target_role="worker-1",
            project_id="p-alpha",
        )
        assert items[0].metadata.get("project_id") == "p-alpha"

    def test_resolve_thread_manager_for_routing(self) -> None:
        """产出回写管理器路由（项目/company/缺省/resolver 异常回落）。"""
        from bandi.company.company_mode import resolve_thread_manager_for
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        company = object()
        project = object()

        def _item(project_id: str = "") -> object:
            metadata = {"project_id": project_id} if project_id else {}
            return create_work_item(
                title="t",
                turn_type=WorkItemTurnType.EXECUTE,
                description="",
                assignee_role="r",
                metadata=metadata,
            )

        assert (
            resolve_thread_manager_for(
                _item("p-alpha"), default=company, resolver=lambda pid: project
            )
            is project
        )
        assert (
            resolve_thread_manager_for(_item("p-alpha"), default=company, resolver=None) is company
        )
        assert (
            resolve_thread_manager_for(
                _item("company"), default=company, resolver=lambda pid: project
            )
            is company
        )
        assert (
            resolve_thread_manager_for(_item(), default=company, resolver=lambda pid: project)
            is company
        )

        def _boom(pid: str) -> object:
            raise RuntimeError("resolver down")

        assert (
            resolve_thread_manager_for(_item("p-alpha"), default=company, resolver=_boom) is company
        )
        assert (
            resolve_thread_manager_for(_item("p-alpha"), default=company, resolver=lambda pid: None)
            is company
        )


class TestProjectWsRouting:
    """WS 级：/ws/company 按 project_id 路由（帧携带 + 落盘隔离 + 补发）。"""

    def test_project_message_lands_in_project_comms(self) -> None:
        _create_project()
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json(
                {"content": "项目群第一条", "sender_role": "user", "project_id": "p-alpha"}
            )
            _receive_until(ws, "ok")

        inbox = client.get(
            "/api/company/inbox", params={"role_id": "board", "project_id": "p-alpha"}
        ).json()
        # （api/app.py `_maybe_secretary_group_reply`——「只有秘书没有团队」时群聊不空转）。
        # 故此处不锁死条数，改钉「用户消息必须落项目空间 + 除秘书外无其他角色越权应答」。
        assert any("项目群第一条" in m["content"] for m in inbox), (
            f"项目群消息必须落项目空间 inbox，实际={inbox}"
        )
        senders = {m.get("sender_role") for m in inbox}
        assert senders <= {"user", "secretary"}, (
            f"项目空间只应出现用户消息与秘书应答，实际发送方={senders}"
        )
        company = client.get("/api/company/inbox", params={"role_id": "board"}).json()
        assert company == [], "项目消息不得落 company 空间（隔离）"

    def test_broadcast_frame_carries_project_id(self) -> None:
        _create_project()
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws_a:
            with client.websocket_connect(
                "/ws/company", headers={"authorization": "Bearer test"}
            ) as ws_b:
                ws_a.send_json({"content": "大家好", "project_id": "p-alpha"})
                frame_a = _receive_until(ws_a, "message")
                frame_b = _receive_until(ws_b, "message")
                assert frame_a["data"]["project_id"] == "p-alpha"
                assert frame_b["data"]["project_id"] == "p-alpha"
                assert frame_a["data"]["body"] == "大家好"

    def test_dm_over_ws_builds_card_and_thread(self) -> None:
        _create_project()
        resp = client.post("/api/company/teams/selection-team/instantiate", json={})
        assert resp.status_code == 200, resp.text
        target = "selection-team_selection_lead"
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json(
                {
                    "content": "单聊派活",
                    "sender_role": "user",
                    "project_id": "p-alpha",
                    "target_role": target,
                }
            )
            ok = _receive_until(ws, "ok")
            assert ok["created"], "单聊必须建卡"
            assert ok["project_id"] == "p-alpha"
            thread = ok.get("thread") or {}
            assert sorted(thread.get("participants") or []) == sorted(["user", target]), (
                f"单聊线程参与者必须为 [user, target]，实际 {thread}"
            )

        items = client.get(
            "/api/company/work-items", params={"role": target, "hide_chat": False}
        ).json()["items"]
        assert items, "单聊卡必须可经 API 查回"
        assert items[0]["assignee_role"] == target

    def test_unknown_project_rejected(self) -> None:
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"content": "幽灵项目", "project_id": "ghost-project"})
            err = _receive_until(ws, "error")
            assert "project not found" in str(err["data"])

    def test_ws_ping_keepalive_pong(self) -> None:
        """修复：两条 WS 通道均应答心跳 ping（前端 判活；否则周期重连风暴）。"""
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as shell_ws:
            shell_ws.send_json({"type": "ping", "ts": 1})
            # -连接首帧为能力握手 hello（断言放宽为「能收到 pong」，不看序位）
            frame = _receive_until(shell_ws, "pong")
            assert frame["type"] == "pong", f"壳级通道必须应答 pong，实际 {frame}"

        with client.websocket_connect(
            "/api/agent/stream", headers={"authorization": "Bearer test"}
        ) as session_ws:
            session_ws.send_json({"type": "ping"})
            frame = session_ws.receive_json()
            assert frame["type"] == "pong", f"会话通道必须应答 pong，实际 {frame}"

    def test_explicit_company_project_routes_to_singleton(self) -> None:
        """显式 project_id=company → 走 company 单例（与缺省行为一致，落 company inbox）。"""
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"content": "公司消息", "sender_role": "user", "project_id": "company"})
            ok = _receive_until(ws, "ok")
            assert ok["project_id"] == "company"
        inbox = client.get("/api/company/inbox", params={"role_id": "board"}).json()
        assert any("公司消息" in m["content"] for m in inbox)

    def test_project_threads_rest_route(self) -> None:
        """修复：项目线程 REST 可查——WS 建线程 → 带 project_id 可查；company 空间隔离。"""
        _create_project()
        resp = client.post("/api/company/teams/selection-team/instantiate", json={})
        assert resp.status_code == 200, resp.text
        target = "selection-team_selection_lead"
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"content": f"@{target} 你好", "project_id": "p-alpha"})
            ok = _receive_until(ws, "ok")
            thread_id = ok["thread_id"]
            assert thread_id, "群聊 @ 必须建线程"

        listed = client.get("/api/company/threads", params={"project_id": "p-alpha"}).json()
        assert any(t["thread_id"] == thread_id for t in listed["threads"]), (
            f"项目空间必须可见该线程，实际 {listed}"
        )
        detail = client.get(f"/api/company/threads/{thread_id}", params={"project_id": "p-alpha"})
        assert detail.status_code == 200, detail.text
        assert detail.json()["messages"], "线程必须含 @ 消息"
        missed = client.get(f"/api/company/threads/{thread_id}")
        assert missed.status_code == 404, "company 空间不得看到项目线程（隔离）"

    def test_thread_followup_without_mention_appends(self, tmp_path: Path) -> None:
        """修复：带 thread_id 的跟帖（无 @）也必须追加到线程（ThreadView 回复路径）。"""
        from bandi.api.company_chat import handle_chat_message
        from bandi.company.discussion_thread import ThreadManager
        from bandi.company.store import WorkItemStore

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _mk_org("worker-1")
        comm = _mk_comms(tmp_path, org)
        thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
        first = handle_chat_message(
            content="@worker-1 你好",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
        )
        thread_id = first[0].metadata["thread_id"]
        handle_chat_message(
            content="补充一句（无 @ 跟帖）",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            thread_id=thread_id,
        )
        thread = thread_mgr.get_thread(thread_id)
        assert thread is not None and len(thread.messages) == 2, (
            f"跟帖必须追加线程，实际 {len(thread.messages) if thread else 'None'}"
        )
        assert thread.messages[-1].content == "补充一句（无 @ 跟帖）"

    def test_breaker_invalid_project_id_not_crash(self) -> None:
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"content": "非法项目", "project_id": "a b"})
            err = _receive_until(ws, "error")
            assert "project" in str(err["data"]).lower()
            # 连接仍可用：后续合法消息正常处理
            ws.send_json({"content": "公司消息", "sender_role": "user"})
            _receive_until(ws, "ok")

    def test_unregistered_target_over_ws_rejected(self) -> None:
        _create_project()
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json(
                {
                    "content": "幽灵单聊",
                    "project_id": "p-alpha",
                    "target_role": "ghost-role",
                }
            )
            err = _receive_until(ws, "error")
            assert "target_role" in str(err["data"])

    def test_resume_replays_frames_with_project_id(self) -> None:
        _create_project()
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json({"content": "补发消息", "project_id": "p-alpha"})
            frame = _receive_until(ws, "message")
            assert frame["data"]["project_id"] == "p-alpha"
            last_seq = int(frame["seq"])
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws2:
            ws2.send_json({"type": "resume", "last_seq": last_seq - 1})
            replayed = []
            while True:
                frame = ws2.receive_json()
                if frame.get("type") == "resumed":
                    break
                replayed.append(frame)
            assert any(f.get("data", {}).get("project_id") == "p-alpha" for f in replayed), (
                f"补发帧必须携带 project_id，实际 {replayed}"
            )

    def test_project_thread_reply_writeback_route(self) -> None:
        """E2E：项目卡产出回写走项目线程（最终产物 = 落盘 hydrate 读回）。"""
        import importlib

        from bandi.company.company_mode import resolve_thread_manager_for
        from bandi.company.discussion_thread import ThreadManager

        _create_project()
        resp = client.post("/api/company/teams/selection-team/instantiate", json={})
        assert resp.status_code == 200, resp.text
        target = "selection-team_selection_lead"
        with client.websocket_connect(
            "/ws/company", headers={"authorization": "Bearer test"}
        ) as ws:
            ws.send_json(
                {
                    "content": "派活",
                    "sender_role": "user",
                    "project_id": "p-alpha",
                    "target_role": target,
                }
            )
            ok = _receive_until(ws, "ok")
            thread_id = ok["thread_id"]
            item_id = ok["created"][0]

        app_module = importlib.import_module("bandi.api.app")
        dispatcher = app_module._get_company_dispatcher()
        assert callable(getattr(dispatcher, "thread_manager_resolver", None)), (
            "dispatcher 必须装配项目线程 resolver"
        )
        item = dispatcher.store.get(item_id)
        manager = resolve_thread_manager_for(
            item,
            default=dispatcher.threads,
            resolver=dispatcher.thread_manager_resolver,
        )
        assert manager is not dispatcher.threads, "项目卡必须路由到项目线程管理器"
        manager.add_message(thread_id, sender=target, content="产出回复", message_id=item_id)

        fresh = ThreadManager(
            workspace=str(Path.home() / ".bandi" / "company"),
            project_id="p-alpha",
            session_id="default",
        )
        fresh.hydrate()
        thread = fresh.get_thread(thread_id)
        assert thread is not None and any(m.content == "产出回复" for m in thread.messages), (
            "项目线程必须能 hydrate 读回产出"
        )
        company_threads = ThreadManager(
            workspace=str(Path.home() / ".bandi" / "company"),
            project_id="company",
            session_id="default",
        )
        company_threads.hydrate()
        assert company_threads.get_thread(thread_id) is None, "产出不得写进 company 线程空间"


def test_thread_and_session_writeback_persist_process_events(tmp_path: Path) -> None:
    """契约：过程快照随回写持久化——线程消息 hydrate 读回 + 会话条目 metadata 带回
    （刷新后单聊/群聊过程块可回看）。"""
    import json as _json

    from bandi.api.company_chat import handle_chat_message
    from bandi.company.session_writeback import write_employee_reply_to_session
    from bandi.memory.session.store import SessionStore

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    comm = _mk_comms(tmp_path, org)
    thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    items = handle_chat_message(
        content="请处理这个",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=thread_mgr,
        target_role="worker-1",
    )
    thread_id = str(items[0].metadata.get("thread_id") or "")
    assert thread_id, "单聊卡必须绑定线程"
    events = [
        {"type": "tool_call_start", "data": {"tool_name": "read", "tool_call_id": "t1"}},
        {
            "type": "tool_result",
            "data": {
                "tool_name": "read",
                "success": True,
                "output_preview": "ok",
                "duration_ms": 5,
                "tool_call_id": "t1",
            },
        },
    ]
    thread_mgr.add_message(
        thread_id, sender="worker-1", content="产出", message_id="wi-1", process_events=events
    )

    fresh = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    fresh.hydrate()
    thread = fresh.get_thread(thread_id)
    assert thread is not None
    target = [m for m in thread.messages if m.message_id == "wi-1"]
    assert target and target[0].process_events == events, "线程消息必须读回过程快照"

    session_store = SessionStore(str(tmp_path / "sessions.db"))
    session_store.create_session(session_id="sess-p1")
    entry_id = write_employee_reply_to_session(
        session_store,
        "sess-p1",
        sender_role="worker-1",
        content="产出",
        work_item_id="wi-1",
        sender_name="工人",
        process_events=events,
    )
    assert entry_id, "回写必须落条目"
    entry = session_store.get_entry(entry_id, "sess-p1")
    assert entry is not None
    meta = _json.loads(entry["metadata"])
    assert meta["process_events"] == events, "会话条目 metadata 必须带回过程快照"


def test_team_session_chat_takeover_routes_to_company() -> None:
    """契约：项目有团队的会话消息由公司路由接管——无 @ 由顶部角色承接建卡、
    用户消息落库（刷新不丢）、终帧 team_routed（不再跑通用助理回合）。"""
    resp = client.post("/api/company/teams/selection-team/instantiate", json={})
    assert resp.status_code == 200, resp.text
    _create_project("p-takeover", "接管项目")
    resp = client.post("/api/company/projects/p-takeover/teams", json={"team_id": "selection-team"})
    assert resp.status_code == 200, resp.text

    with client.websocket_connect(
        "/api/agent/stream", headers={"authorization": "Bearer test"}
    ) as ws:
        ws.send_json(
            {
                "type": "chat",
                "message": "你好",
                "session_id": "sess-takeover-1",
                "project_id": "p-takeover",
                "mode": "execute",
            }
        )
        done = _receive_until(ws, "done")
        assert done["data"]["terminal_reason"] == "team_routed", done

    items = client.get("/api/company/work-items", params={"hide_chat": False}).json()["items"]
    assert any(
        str(item.get("assignee_role") or "").startswith("selection-team_") for item in items
    ), f"无 @ 必须由顶部角色承接建卡，实际 {items}"
    detail = client.get("/api/sessions/sess-takeover-1").json()
    contents = [str(m.get("content") or "") for m in detail.get("messages") or []]
    assert "你好" in contents, f"用户消息必须落库可回看，实际 {contents}"


def test_team_session_takeover_skips_teamless_project() -> None:
    """破坏者：无团队项目不得被接管——回落通用助理回合（此处以入参校验阻断，
    断言会话消息未落公司路由：建卡为零）。"""
    _create_project("p-plain", "无团队项目")
    with client.websocket_connect(
        "/api/agent/stream", headers={"authorization": "Bearer test"}
    ) as ws:
        ws.send_json(
            {
                "type": "chat",
                "message": "你好",
                "session_id": "sess-plain-1",
                "project_id": "p-plain",
                "mode": "execute",
            }
        )
        # 通用助理回合在无模型配置下走完（可能先发 note/error 等帧）——收到任一终态即可
        frame: dict = {}
        for _ in range(12):
            frame = ws.receive_json()
            if frame.get("type") in ("done", "error"):
                break
        assert frame.get("type") in ("done", "error"), frame
    items = client.get("/api/company/work-items", params={"hide_chat": False}).json()["items"]
    assert not any(
        str(item.get("assignee_role") or "").startswith("selection-team_") for item in items
    ), "无团队项目不得触发公司建卡"
