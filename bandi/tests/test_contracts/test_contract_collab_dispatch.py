# 契约原则：binding 构建可靠 / send_dm 落盘可读回 / inbox 反映实际消息数 /
# build_runtime_from_env → CommunicationManager 方法
from __future__ import annotations

import asyncio

from bandi.company.phase import Phase
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.tools.collaboration_dispatch import (
    BOUND_HANDLERS,
    HANDLERS,
    build_collaboration_runtime_from_env,
    dispatch_collaboration_tool,
)


def _make_env(tmp_path, role: str = "worker") -> dict[str, str]:
    return {
        "BANDI_WORKSPACE_ROOT": str(tmp_path),
        "BANDI_COMMS_PROJECT": "p1",
        "BANDI_COMMS_SESSION": "s1",
        "BANDI_COMMS_FROM": role,
    }


def test_contract_binding_constructs_from_env(tmp_path) -> None:
    """契约 1：build_collaboration_runtime_from_env 必须从 env 构建
    含 comms + context 的 binding，role_id 取 BANDI_COMMS_FROM。"""
    env = _make_env(tmp_path, role="alice")
    svc, ctx, store, comms, _org = asyncio.run(build_collaboration_runtime_from_env(env))
    assert ctx.role_id == "alice"
    assert ctx.project_id == "p1"
    assert ctx.session_id == "s1"
    assert comms is not None


def test_contract_send_dm_durable(tmp_path) -> None:
    """契约 2：send_dm 通过 dispatch 必须落盘到收件人文件系统收件箱——
    收件人以自身 role_id 调用 inbox 可读到该消息（真实 I/O 而非内存 mock）。"""
    # Worker 发给 Bob，然后用 Bob 的角色读取
    env_worker = _make_env(tmp_path, role="worker")
    env_bob = _make_env(tmp_path, role="bob")

    result, is_error = asyncio.run(
        dispatch_collaboration_tool(
            "send_dm",
            {"to_agent": "bob", "subject": "周会", "body": "下午三点"},
            env=env_worker,
        )
    )
    assert not is_error
    assert result.get("to_agent") == "bob"

    result2, err2 = asyncio.run(
        dispatch_collaboration_tool("inbox", {"action": "status"}, env=env_bob)
    )
    assert not err2
    assert result2.get("unread_count", 0) >= 1


def test_contract_inbox_reflects_sent_messages(tmp_path) -> None:
    """契约 3：inbox 计数必须与实际发件一致——连续发 N 条后 inbox 返回 N。"""
    env_worker = _make_env(tmp_path, role="worker")
    env_charlie = _make_env(tmp_path, role="charlie")
    for i in range(3):
        asyncio.run(
            dispatch_collaboration_tool(
                "send_dm",
                {"to_agent": "charlie", "subject": f"消息{i}", "body": f"内容{i}"},
                env=env_worker,
            )
        )
    result, _ = asyncio.run(
        dispatch_collaboration_tool("inbox", {"action": "status"}, env=env_charlie)
    )
    assert result.get("unread_count", 0) >= 3


def test_contract_error_propagates_not_swallowed(tmp_path) -> None:
    """契约 4：dispatch 遇到未知工具必须返回 error dict 且 is_error=True
    不得抛未处理异常。"""
    result, is_error = asyncio.run(
        dispatch_collaboration_tool("nonexistent_xyz", {}, env=_make_env(tmp_path))
    )
    assert is_error is True
    assert "error" in result
    assert isinstance(result["error"], str) and len(result["error"]) > 0


def test_contract_fail_closed_when_no_env(tmp_path) -> None:
    """契约 5：无 env 时 dispatch 必须 fail-closed（返回 error，不崩溃）。"""
    result, is_error = asyncio.run(
        dispatch_collaboration_tool(
            "send_dm",
            {"to_agent": "m", "subject": "s", "body": "b"},
        )
    )
    assert is_error is True
    assert "error" in result


def test_contract_all_handlers_registered() -> None:
    """契约 6：BOUND_HANDLERS 和 HANDLERS 必须包含全部 9 个协作工具名。"""
    expected = {
        "inbox",
        "send_dm",
        "ask_peer_and_wait",
        "reply_message",
        "broadcast_issue",
        "start_meeting",
        "respond_meeting",
        "route_work",
        "close_human_review",
    }
    assert set(BOUND_HANDLERS.keys()) == expected
    assert set(HANDLERS.keys()) == expected


def test_contract_allowed_tools_enforced(tmp_path) -> None:
    """契约 7：BANDI_ALLOWED_COLLAB_TOOLS=「[]」时所有工具必须被拒绝——
    权限白名单是安全机制，不是装饰性字段。"""
    env = _make_env(tmp_path)
    env["BANDI_ALLOWED_COLLAB_TOOLS"] = "[]"
    result, is_error = asyncio.run(
        dispatch_collaboration_tool(
            "send_dm", {"to_agent": "m", "subject": "s", "body": "b"}, env=env
        )
    )
    assert is_error is True
    assert "error" in result
    # 确认消息未落盘（无 msg_id）
    assert "msg_id" not in result


def _make_org():
    from bandi.company.org_engine import OrgEngine

    engine = OrgEngine()
    engine.register_role("ceo", "CEO")
    engine.register_role("manager", "Manager", reports_to="ceo")
    engine.register_role("worker", "Worker", reports_to="manager")
    return engine


def test_contract_comms_topology_blocks_cross_role_send(tmp_path) -> None:
    """
    直属上级放行——校验下沉通信层，工具/dispatch/CLI 全路径自动覆盖。"""
    from bandi.company.communication import CommunicationManager

    org = _make_org()
    comms = CommunicationManager(str(tmp_path), "p1", "s1", org_engine=org)
    msg_id = comms.send_dm("worker", "manager", "周会", "下午三点")
    assert msg_id
    try:
        comms.send_dm("worker", "ceo", "越级", "绕过经理")
        raise AssertionError("worker→ceo 越级发送未被拦截")
    except ValueError as exc:
        assert "cannot message" in str(exc)


def test_contract_dispatch_guard_covers_ask_peer_and_wait(tmp_path) -> None:
    """
    org 注入时跨拓扑阻塞等待被拒，不得绕过 collaboration_allowed。"""
    env = _make_env(tmp_path, role="worker")
    result, is_error = asyncio.run(
        dispatch_collaboration_tool(
            "ask_peer_and_wait",
            {"to_agent": "ceo", "subject": "s", "body": "b"},
            env=env,
            org_engine=_make_org(),
        )
    )
    assert is_error, f"ask_peer_and_wait 越级未被拦截: {result}"
    assert "not allowed" in result.get("error", "")


def test_contract_reply_requires_recipient_relationship(tmp_path) -> None:
    """
    。"""
    from bandi.company.communication import CommunicationManager

    comms = CommunicationManager(str(tmp_path), "p1", "s1")
    msg_id = comms.send_dm("alice", "bob", "问题", "请回复")
    reply_id = comms.reply_to_message(msg_id, "bob", "收到")
    assert reply_id
    try:
        comms.reply_to_message(msg_id, "carly", "劫持")
        raise AssertionError("非收件人 reply 未被拦截")
    except ValueError as exc:
        assert "cannot reply" in str(exc)


def test_contract_close_human_review_requires_flag(tmp_path) -> None:
    """
    ——默认拒绝。"""
    env = _make_env(tmp_path, role="worker")
    result, is_error = asyncio.run(
        dispatch_collaboration_tool(
            "close_human_review",
            {"summary": "关闭评审", "target_role": "owner"},
            env=env,
        )
    )
    assert is_error, f"无授权标记的 close_human_review 未被拦截: {result}"


def test_contract_meeting_room_survives_restart(tmp_path) -> None:
    """
    respond/finalize 可用，卡的 meeting peer_wait 不再永久滞留。"""
    from bandi.company.communication import CommunicationManager

    comms1 = CommunicationManager(str(tmp_path), "p1", "s1")
    room_id = comms1.create_room(["alice", "bob"], "方案评审")
    # 模拟进程重启：全新实例（内存 _rooms 为空）
    comms2 = CommunicationManager(str(tmp_path), "p1", "s1")
    room = comms2.get_room(room_id)
    assert room is not None, "重启后会议房间丢失（仅存内存）"
    assert room.participants == ["alice", "bob"]
    assert room.agenda == "方案评审"
    # 恢复后的房间可正常发帖与 finalize
    assert comms2.respond_to_meeting(room_id, "alice", "同意") is True
    assert comms2.respond_to_meeting(room_id, "bob", "通过", finalize=True) is True
    finalized = comms2.get_room(room_id)
    assert finalized is not None and finalized.status.value == "decided"


def test_contract_meeting_wait_survives_restart_with_deadline(tmp_path) -> None:
    """
    重启后恢复完整状态——超时兜底 auto_resolve 不因 deadline 丢失而失效。"""
    from bandi.company.communication import CommunicationManager

    comms1 = CommunicationManager(str(tmp_path), "p1", "s1")
    task = create_work_item(
        "评审卡", WorkItemTurnType.EXECUTE, assignee_role="alice", phase=Phase.RUNNING
    )
    result = comms1.open_meeting_wait(
        task, "架构评审", ["alice", "bob"], "定方案", decision_owner="alice", timeout_seconds=900
    )
    room_id = result["meeting_room_id"]
    assert task.metadata["peer_wait"]["timeout_at"] > 0

    comms2 = CommunicationManager(str(tmp_path), "p1", "s1")
    room = comms2.get_room(room_id)
    assert room is not None, "重启后会议丢失"
    assert room.status.value == "in_progress", f"状态未持久化: {room.status}"
    assert room.deadline_at is not None, "deadline 未持久化（超时兜底失效）"
    assert room.pending_participants == ["alice", "bob"]
    assert room.decision_owner == "alice"
