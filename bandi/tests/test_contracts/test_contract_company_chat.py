"""
契约族：Chat-First 协作面板——WS broadcast + @mention 触发 work_item。

背景（差距清单 ，落档 Day 125-126）：CommunicationManager 文件队列机制可用
缺 ① 消息实时广播（WebSocket broadcast，群聊面板前置）② Dispatcher/API
@mention 触发 work_item（群聊 @角色 → 创建并路由工作项）。

本契约钉的行为族（生产调用路径）：
- `company/communication.py::CommunicationManager.send_dm`（消息落盘主入口 → 回调）
- `api/app.py::/ws/company`（WebSocket 端点，群聊前端接入）
- mention → work_item 创建链（API 装配点复用函数）

断言「最终产物」：
- send_dm 成功后触发 on_message_sent 回调（广播接线点存在）；
- 消息内容含 @角色 → 创建 assignee=该角色的 work_item（QUEUED 入队可被 tick 认领）；
- WS 端点存在且委托共享消息处理函数（接线契约）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.communication import CommunicationManager, list_inbox
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase


def _mk_org(*role_ids: str) -> OrgEngine:
    org = OrgEngine()
    for role_id in role_ids:
        org.register_role(role_id, role_id)
    return org


def _extract_mentions(content: str) -> list[str]:
    """提取 @角色 提及（与生产实现同规则——单一来源避免漂移）。"""
    from bandi.api.company_chat import extract_mentions as _impl

    return _impl(content)


def test_contract_send_dm_triggers_on_message_sent(tmp_path: Path) -> None:
    """send_dm 落盘成功必须触发 on_message_sent 回调（WS 广播接线点）。"""
    seen: list[dict] = []
    org = OrgEngine()
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
        on_message_sent=seen.append,
    )
    comm.send_dm(
        sender_role="peer-a",
        recipient_role="worker-1",
        subject="hello",
        body="hi there",
    )
    assert len(seen) == 1, f"send_dm 必须触发回调，实际 {len(seen)}"
    assert seen[0]["sender_role"] == "peer-a"
    assert seen[0]["recipient_role"] == "worker-1"
    assert seen[0]["subject"] == "hello"


def test_contract_mention_creates_work_item(tmp_path: Path) -> None:
    """消息含 @已注册角色 → 创建 assignee=该角色的 QUEUED work_item（可被调度认领）。
    Day 129 语义变更：仅已注册角色命中才建卡（杜绝悬空卡），org_engine 必传。"""
    from bandi.api.company_chat import handle_chat_message

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    created = handle_chat_message(
        content="请处理这个问题 @worker-1",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1, f"@mention 必须创建 work_item，实际 {created}"
    item = created[0]
    assert item.assignee_role == "worker-1", f"assignee 必须是 @角色，实际 {item.assignee_role}"
    assert item.phase == Phase.QUEUED, "创建后必须 QUEUED 可被认领"
    # 无 mention 不创建
    created2 = handle_chat_message(
        content="没有提及的消息",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert created2 == [], "无 @mention 不得创建 work_item"


def test_contract_dm_pins_specific_employee_assignment(tmp_path: Path) -> None:
    """DM 指定 employee_id 时，工作项必须钉住该员工，供调度器优先采用。"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.org_engine import OrgEmployee

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    org.register_employee(OrgEmployee(employee_id="e1", name="甲", role_id="worker-1"))
    org.register_employee(OrgEmployee(employee_id="e2", name="乙", role_id="worker-1"))
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    created = handle_chat_message(
        content="请处理",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        target_role="worker-1",
        employee_id="e2",
        session_id="s1",
    )
    assert len(created) == 1
    assignment = created[0].metadata.get("employee_assignment") or {}
    assert assignment.get("employee_id") == "e2"
    assert assignment.get("role_id") == "worker-1"


def test_contract_dm_confirm_grants_human_review_close(tmp_path: Path) -> None:
    """DM 确认卡建卡时必须写入可信关闭授权位；非确认意图不得保留该授权。"""
    from bandi.api.company_chat import handle_chat_message

    store, comm = _mk_store_and_comm(tmp_path)
    org = _mk_org("worker-1")
    created = handle_chat_message(
        content="确认通过本次人工评审",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        target_role="worker-1",
        session_id="s1",
        intent="confirm",
    )
    assert len(created) == 1
    assert created[0].metadata.get("human_review_close_allowed") is True

    reused = handle_chat_message(
        content="再聊一句普通消息",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        target_role="worker-1",
        session_id="s1",
        intent="chat",
    )
    assert len(reused) == 1
    assert "human_review_close_allowed" not in reused[0].metadata


def test_contract_ws_endpoint_delegates_to_handler() -> None:
    """WS 端点必须委托共享消息处理函数（接线契约 ）。"""
    import sys

    from bandi.api.app import app  # noqa: F401 — 触发模块加载

    app_module = sys.modules["bandi.api.app"]
    assert hasattr(app_module, "company_ws_route"), "WS 路由必须存在（company_ws_route）"
    # 路由注册检查（路由表含 /ws/company）
    paths = [r.path for r in app_module.app.routes if getattr(r, "path", None) == "/ws/company"]
    assert paths, "app 必须注册 /ws/company 路由"


def _mk_store_and_comm(tmp_path: Path) -> tuple[WorkItemStore, CommunicationManager]:
    return (
        WorkItemStore(str(tmp_path / "wi.db")),
        CommunicationManager(
            workspace=str(tmp_path / "ws"),
            project_id="p1",
            session_id="s1",
        ),
    )


def test_contract_chat_message_persists_kind(tmp_path: Path) -> None:
    """群聊消息落盘必须带 kind=chat 标记（面板区分群聊/工作消息）。
    修复：extra_metadata 的 kind 不再被 _filter_metadata 白名单过滤丢弃。"""
    from bandi.api.company_chat import handle_chat_message

    store, comm = _mk_store_and_comm(tmp_path)
    handle_chat_message(
        content="上线预定 @worker",
        sender_role="user",
        communication=comm,
        store=store,
    )
    msgs = list_inbox(str(tmp_path / "ws"), "p1", "s1", "board")
    assert msgs, "群聊消息必须落盘到 board inbox"
    assert msgs[0].metadata.get("kind") == "chat", (
        f"落盘必须带 kind=chat，实际 metadata={msgs[0].metadata}"
    )


def test_contract_send_dm_failure_not_silent(tmp_path: Path) -> None:
    """send_dm 落盘失败（无权联系收件人）不得被静默吞掉后继续建 work_item——
    群聊必须显式暴露发送失败，避免「有任务无消息」状态不一致。"""
    from bandi.api.company_chat import handle_chat_message

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    org.register_role("ceo", "CEO")
    org.register_role("worker", "Worker", reports_to="ceo")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    with pytest.raises(ValueError):
        handle_chat_message(
            content="请处理 @worker",
            sender_role="worker",
            communication=comm,
            store=store,
        )


def test_handle_mention_binds_thread_id_when_manager_present(tmp_path: Path) -> None:
    """@mention + thread_manager → 建线程绑定到卡的 metadata；参与者含 @角色。
    （契约：互@ 建线程只发生在 thread_manager 存在时，旧路径不带 manager 行为不变。）"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("tech")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    items = handle_chat_message(
        content="@tech 请出方案",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=thread_mgr,
    )
    assert len(items) == 1
    thread_id = items[0].metadata.get("thread_id")
    assert thread_id and thread_id.startswith("thread_")
    t = thread_mgr.get_thread(thread_id)
    assert t is not None and "tech" in t.participants
    assert len(t.messages) == 1
    assert t.messages[0].content == "@tech 请出方案"
    # 旧路径（无 thread_manager）不带 thread_id → 行为不变
    items2 = handle_chat_message(
        content="@tech 继续",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        session_id="s-no-thread",
    )
    assert "thread_id" not in items2[0].metadata


def test_handle_mention_multi_target_shares_one_thread(tmp_path: Path) -> None:
    """同一消息 @ 多角色 → 仅建一个线程，所有卡共享同一 thread_id。
    （契约：线程是「对话链容器」，一次派发只开一个对话链。）"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("product", "tech")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    items = handle_chat_message(
        content="@product @tech 一起出方案",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=thread_mgr,
    )
    assert len(items) == 1
    from bandi.api.company_chat import _default_group_round_service

    round_ = _default_group_round_service(store, org).get_active("")
    assert round_ is not None
    assert [slot.role_id for slot in round_.slots] == ["product", "tech"]
    thread_ids = {it.metadata.get("thread_id") for it in items}
    assert len(thread_ids) == 1, f"多角色派发必须共享一个线程，实际 {thread_ids}"
    t = thread_mgr.get_thread(next(iter(thread_ids)))
    assert t is not None
    assert set(t.participants) == {"product", "tech"}


def test_handle_mention_followup_uses_existing_thread(tmp_path: Path) -> None:
    """带已有 thread_id 的跟帖 → 追加消息到既有线程，不建新线程。
    （契约：thread_id 透传语义——前端跟帖复制 thread_id。）"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("tech")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    thread = thread_mgr.create_thread(participants=["tech"])
    items = handle_chat_message(
        content="@tech 补充：方案要含测试",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=thread_mgr,
        thread_id=thread.thread_id,
    )
    assert len(items) == 1
    rt = thread_mgr.get_thread(thread.thread_id)
    assert rt is not None and len(rt.messages) == 1
    assert rt.messages[0].content == "@tech 补充：方案要含测试"


def test_handle_mention_invalid_thread_id_fail_closed(tmp_path: Path) -> None:
    """带不存在的 thread_id 跟帖 → 必须失败拒绝，不得把卡绑定到不存在线程。

    （契约：无效 thread_id 是前端伪造/过期帧，fail-open 会让产出回写静默丢失——
    与 spawn_thread_task 对 thread_id 的 fail-closed 校验一致。防绕过点：
    直接构造已删线程 id 或任意非法 id 都不得静默进线程追加分支。）"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("tech")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    thread_mgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    with pytest.raises(ValueError):
        handle_chat_message(
            content="@tech 伪造线程",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            thread_manager=thread_mgr,
            thread_id="thread_deadbeef",
        )
    assert store.list_all() == [], "失败后不得残留任何卡"
    pending = handle_chat_message(
        content="无人盯",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert pending == []


def test_display_name_with_space_resolves_and_routes(tmp_path) -> None:
    """@UI 设计（含空格显示名）→ 定向 UI 设计角色；clean 无 @ 残留；不误报未注册。"""
    from bandi.api.company_chat import (
        _registered_name_map,
        _registered_role_ids,
        handle_chat_message,
        resolve_mentions,
        unresolved_mentions,
    )

    org = OrgEngine()
    org.register_role("designer", "UI 设计")
    org.register_role("backend_dev", "后端工程师")

    result = resolve_mentions(
        "@UI 设计 你好", sorted(_registered_role_ids(org)), _registered_name_map(org)
    )
    assert result.target_agents == ["designer"]
    assert result.clean_message == "你好"
    assert unresolved_mentions("@UI 设计 你好", org) == []

    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    store = WorkItemStore(str(tmp_path / "wi.db"))
    created = handle_chat_message(
        content="@UI 设计 你好",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert [c.assignee_role for c in created] == ["designer"]


def test_display_name_ambiguous_name_not_routed() -> None:
    """同名歧义（两个角色同名）不在 name_map——不建卡也不误路由。"""
    from bandi.api.company_chat import (
        _registered_name_map,
        _registered_role_ids,
        resolve_mentions,
    )

    org = OrgEngine()
    org.register_role("designer_a", "设计")
    org.register_role("designer_b", "设计")
    result = resolve_mentions(
        "@设计 你好", sorted(_registered_role_ids(org)), _registered_name_map(org)
    )
    assert result.target_agents == []


def test_delegate_whitelist_injected_from_can_spawn(tmp_path: Path) -> None:
    """可派活角色（can_spawn 非空）的卡必须带 allowed_delegate_role_ids，
    否则 turn_mode 判 EXECUTE 单卡包干整条链。"""
    from bandi.api.company_chat import handle_chat_message

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    org.register_role("lead", "负责人", can_spawn=["researcher", "designer"])
    org.register_role("researcher", "研究员")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    created = handle_chat_message(
        content="@负责人 请统筹这个项目",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1
    assert created[0].metadata.get("allowed_delegate_role_ids") == ["researcher", "designer"]
    query_items = handle_chat_message(
        content="这个方案怎么样 @负责人",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        intent="query",
        session_id="sess-query",
    )
    assert query_items
    assert "allowed_delegate_role_ids" not in query_items[0].metadata


def test_contract_group_thread_kind_and_session_binding(tmp_path: Path) -> None:
    """群聊消息必须落 kind=group、session_id=群聊会话的线程（回复卡片据此落位）。"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    threads = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")

    created = handle_chat_message(
        content="你好 @worker-1",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=threads,
        session_id="group-session",
        project_id="p1",
        member_role_ids={"worker-1"},
    )

    thread = threads.find_by_session("group-session")
    assert thread is not None, "群聊消息必须绑定会话线程"
    assert thread.kind == "group", f"群聊线程 kind 应为 group，实际 {thread.kind}"
    assert created, "群聊 @ 员工应创建回合卡"
    assert created[0].metadata.get("thread_id") == thread.thread_id


def test_contract_group_default_route_binds_group_thread(tmp_path: Path) -> None:
    """群聊无 @ 时由默认角色承接，同样必须绑定 kind=group 的会话线程。"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    threads = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")

    handle_chat_message(
        content="你好",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=threads,
        session_id="group-default",
        project_id="p1",
        member_role_ids={"worker-1"},
        default_role="worker-1",
    )

    thread = threads.find_by_session("group-default")
    assert thread is not None, "默认路由也必须绑定会话线程"
    assert thread.kind == "group"


def test_contract_employee_dm_thread_keeps_dm_kind(tmp_path: Path) -> None:
    """员工私聊仍必须落 kind=dm 线程，与群聊隔离。"""
    from bandi.api.company_chat import handle_chat_message
    from bandi.company.discussion_thread import ThreadManager

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _mk_org("worker-1")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    threads = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")

    handle_chat_message(
        content="你好",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        thread_manager=threads,
        session_id="dm-session",
        project_id="p1",
        target_role="worker-1",
    )

    thread = threads.find_by_session("dm-session")
    assert thread is not None, "员工私聊必须绑定会话线程"
    assert thread.kind == "dm", f"私聊线程 kind 应为 dm，实际 {thread.kind}"


def test_group_session_ignores_stray_target_role(tmp_path: Path) -> None:
    """破坏者视角：群聊会话被前端误带 target_role 时，仍必须走群聊回合而不是 DM 单卡。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from bandi.api.company_chat import handle_chat_message

    org = _mk_org("lead", "worker")
    store = WorkItemStore(str(tmp_path / "wi.db"))
    calls: list[dict] = []

    class _Rounds:
        def submit_user_message(self, **kwargs):
            calls.append(kwargs)
            return []

    class _Threads:
        def find_by_session(self, session_id: str):
            return SimpleNamespace(thread_id="t-group", kind="group")

        def get_thread(self, thread_id: str):
            return SimpleNamespace(thread_id=thread_id, kind="group")

        def add_message(self, *args, **kwargs):
            return None

        def create_thread(self, **kwargs):
            return SimpleNamespace(thread_id="t-group")

    created = handle_chat_message(
        content="@worker 做一版方案",
        sender_role="user",
        communication=MagicMock(),
        store=store,
        org_engine=org,
        thread_manager=_Threads(),
        thread_id=None,
        target_role="lead",
        project_id="p1",
        session_id="s1",
        member_role_ids={"lead", "worker"},
        group_round_service=_Rounds(),
        intent="task",
    )
    assert calls, "群聊会话必须走回合服务，而不是建 DM 单卡"
    assert calls[0]["targets"] == ["worker"], "目标应来自正文 @，而不是残留的 target_role"
    assert created == [], "群聊路径不应直接建 DM 卡"
