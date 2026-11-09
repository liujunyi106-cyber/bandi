# 契约原则：send_dm 落盘可读回 / inbox 未读计数 / reply 关联原消息 /
# ask_peer 超时不无限阻塞 / 无 comms fail-closed / 角色收件箱隔离。
# run_agent_turn → build_agent_context → ToolExecutor.execute → 工具.execute
# （context.communication 由 ToolPermissionContext 透传，装配点 custom_runtime.py:159）
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bandi.company.communication import CommunicationManager
from bandi.tools.base import ToolPermissionContext
from bandi.tools.collaboration import (
    AskPeerAndWaitTool,
    BroadcastIssueTool,
    CloseHumanReviewTool,
    InboxTool,
    ReplyMessageTool,
    RespondMeetingTool,
    RouteWorkTool,
    SendDmTool,
    StartMeetingTool,
)


def _comms(tmp_path) -> CommunicationManager:
    return CommunicationManager(workspace=str(tmp_path), project_id="p1", session_id="s1")


def _ctx(
    comms: CommunicationManager,
    role: str = "worker",
    *,
    authorized: bool = False,
) -> ToolPermissionContext:
    extra = {"human_review_close_allowed": True} if authorized else None
    return ToolPermissionContext(
        user_id=role, role=role, mode="execute", communication=comms, extra=extra
    )


def test_contract_send_dm_persists_to_recipient_inbox(tmp_path) -> None:
    """契约 1：send_dm 必须落盘到收件人收件箱（文件系统可读回）——
    sender 取 context.role，recipient 收件箱 new/ 目录出现消息文件。"""
    comms = _comms(tmp_path)
    tool = SendDmTool()
    result = asyncio.run(
        tool.execute(
            {"to_agent": "manager", "subject": "进度", "body": "任务完成"},
            _ctx(comms, role="worker"),
        )
    )
    assert isinstance(result, dict), f"send_dm 必须返回 dict，实际 {type(result)}"
    assert result.get("to_agent") == "manager"
    inbox = comms.read_inbox("manager", include_seen=True)
    assert inbox, "收件人收件箱必须可读回消息"
    msg = inbox[-1]
    assert msg.sender_role == "worker", f"sender 必须是 context.role，实际 {msg.sender_role}"
    assert msg.subject == "进度" and "任务完成" in msg.body


def test_contract_inbox_unread_count_and_peek(tmp_path) -> None:
    """契约 2：inbox status 返回未读计数、peek 返回消息列表——
    未读消息经 status 计数、经 peek 可见内容（不读即不置已读）。"""
    comms = _comms(tmp_path)
    comms.send_dm("manager", "worker", "会议", "下午开会", blocking=False)
    comms.send_dm("manager", "worker", "提醒", "记得提交", blocking=False)

    status = asyncio.run(InboxTool().execute({"action": "status"}, _ctx(comms, role="worker")))
    assert status["unread_count"] == 2, f"未读计数必须为 2，实际 {status['unread_count']}"
    assert not status.get("messages"), "status 不应带消息体（peek 才带）"

    peek = asyncio.run(InboxTool().execute({"action": "peek"}, _ctx(comms, role="worker")))
    assert peek["unread_count"] == 2
    bodies = [m.get("body", "") for m in peek.get("messages", [])]
    assert any("下午开会" in b for b in bodies), f"peek 必须含消息内容: {bodies}"


def test_contract_reply_message_links_original(tmp_path) -> None:
    """契约 3：reply_message 必须关联原消息——回复落在原发送者收件箱
    且 metadata.reply_to_msg_id 指向原消息 id（reply 链路可追踪）。"""
    comms = _comms(tmp_path)
    original_id = comms.send_dm("manager", "worker", "问题", "请确认", blocking=False)

    result = asyncio.run(
        ReplyMessageTool().execute(
            {"message_id": original_id, "body": "已确认"}, _ctx(comms, role="worker")
        )
    )
    assert isinstance(result, dict) and result.get("msg_id"), f"reply 必须返回新消息 id: {result}"
    manager_inbox = comms.read_inbox("manager", include_seen=True)
    reply = manager_inbox[-1]
    assert reply.metadata.get("reply_to_msg_id") == original_id, (
        f"reply 必须关联原消息，实际 {reply.metadata.get('reply_to_msg_id')!r}"
    )


def test_contract_ask_peer_times_out_bounded(tmp_path) -> None:
    """契约 4：ask_peer_and_wait 超时必须返回（不无限阻塞）——
    无回复时在 timeout_seconds 内返回 reply=None 且耗时受限。"""
    comms = _comms(tmp_path)

    async def _run() -> dict:
        start = asyncio.get_event_loop().time()
        out = await AskPeerAndWaitTool().execute(
            {"to_agent": "manager", "subject": "q", "body": "在吗", "timeout_seconds": 1},
            _ctx(comms, role="worker"),
        )
        return {"out": out, "elapsed": asyncio.get_event_loop().time() - start}

    result = asyncio.run(_run())
    assert result["elapsed"] < 10, f"ask_peer 必须超时返回而非无限阻塞: {result['elapsed']:.1f}s"
    assert result["out"].get("reply") is None, f"无回复必须返回 reply=None: {result['out']}"


_FAIL_CLOSED_CASES = [
    pytest.param(SendDmTool(), {"to_agent": "manager", "subject": "s", "body": "b"}, id="send_dm"),
    pytest.param(InboxTool(), {"action": "status"}, id="inbox"),
    pytest.param(
        AskPeerAndWaitTool(),
        {"to_agent": "manager", "subject": "s", "body": "b"},
        id="ask_peer_and_wait",
    ),
    pytest.param(ReplyMessageTool(), {"message_id": "id", "body": "b"}, id="reply_message"),
    pytest.param(
        BroadcastIssueTool(),
        {"to_agents": ["a"], "subject": "s", "body": "b"},
        id="broadcast_issue",
    ),
    pytest.param(
        StartMeetingTool(), {"topic": "t", "participants": ["a"], "agenda": "g"}, id="start_meeting"
    ),
    pytest.param(RespondMeetingTool(), {"room_id": "r", "content": "c"}, id="respond_meeting"),
    pytest.param(
        RouteWorkTool(), {"action": "send_followup", "target_role": "worker"}, id="route_work"
    ),
    pytest.param(CloseHumanReviewTool(), {"summary": "s"}, id="close_human_review"),
]


@pytest.mark.parametrize("tool,params", _FAIL_CLOSED_CASES)
def test_contract_no_comms_fails_closed(tool, params) -> None:
    """契约 5（族级 parametrize=9）：9 个协作工具无 communication（非公司链路）必须"""
    ctx = ToolPermissionContext(user_id="worker", role="worker", mode="execute", communication=None)
    result = asyncio.run(tool.execute(params, ctx))
    assert isinstance(result, dict) and "error" in result, (
        f"{tool.name} 无 comms 必须返回 error，实际 {result!r}"
    )


def test_contract_role_inbox_isolated(tmp_path) -> None:
    """契约 6：角色收件箱隔离——发给 worker 的消息不得出现在
    manager 收件箱（read_inbox 按 role 过滤不串扰）。"""
    comms = _comms(tmp_path)
    comms.send_dm("manager", "worker", "给 worker", "专属", blocking=False)
    comms.send_dm("worker", "manager", "给 manager", "反向", blocking=False)

    worker_inbox = comms.read_inbox("worker", include_seen=True)
    manager_inbox = comms.read_inbox("manager", include_seen=True)
    assert all(m.sender_role == "manager" for m in worker_inbox)
    assert all(m.sender_role == "worker" for m in manager_inbox)


def test_contract_broadcast_reaches_all_recipients(tmp_path) -> None:
    """契约 7：broadcast_issue 必须送达全部收件人——每个收件人收件箱
    都能读回广播消息（broadcast = send_dm 批量语义）。"""
    comms = _comms(tmp_path)
    result = asyncio.run(
        BroadcastIssueTool().execute(
            {"to_agents": ["a", "b"], "subject": "公告", "body": "全员知晓"},
            _ctx(comms, role="manager"),
        )
    )
    assert isinstance(result, dict), f"broadcast 必须返回 dict: {result}"
    assert len(result.get("sent", [])) == 2, f"必须送达 2 个收件人: {result}"
    for role in ("a", "b"):
        inbox = comms.read_inbox(role, include_seen=True)
        assert any("全员知晓" in m.body for m in inbox), f"{role} 收件箱必须收到广播"


def test_contract_meeting_respond_roundtrip(tmp_path) -> None:
    """契约 8：start_meeting 创建会议房间、respond_meeting 发言可读回——
    respond 追加发言到房间消息列表并返回 ok。"""
    comms = _comms(tmp_path)
    started = asyncio.run(
        StartMeetingTool().execute(
            {"topic": "评审", "participants": ["a", "b"], "agenda": "讨论方案"},
            _ctx(comms, role="manager"),
        )
    )
    room_id = started.get("room_id")
    assert room_id, f"start_meeting 必须返回 room_id: {started}"
    assert set(started.get("participants", [])) == {"a", "b"}

    responded = asyncio.run(
        RespondMeetingTool().execute(
            {"room_id": room_id, "content": "我同意", "finalize": True},
            _ctx(comms, role="a"),
        )
    )
    assert responded.get("ok") is True, f"respond_meeting 必须 ok: {responded}"
    room = comms.get_room(room_id)
    assert room is not None and any("我同意" in m.content for m in room.messages), (
        "发言必须可读回（房间消息列表）"
    )


def test_contract_route_work_and_close_review(tmp_path) -> None:
    """契约 9：route_work/close_human_review 必须完成路由/关闭动作——
    send_followup 落盘给 target_role、escalate 落盘给 target_role
    close_human_review 落盘给 target_role（评审关闭通知）。"""
    comms = _comms(tmp_path)
    ctx = _ctx(comms, role="coordinator", authorized=True)

    follow = asyncio.run(
        RouteWorkTool().execute(
            {"action": "send_followup", "target_role": "worker", "prompt": "加快进度"}, ctx
        )
    )
    assert follow.get("routed_to") == "worker", f"followup 必须路由到 worker: {follow}"
    worker_inbox = comms.read_inbox("worker", include_seen=True)
    assert any("加快进度" in m.body for m in worker_inbox), "followup 必须落盘给 worker"

    bad = asyncio.run(RouteWorkTool().execute({"action": "nope", "target_role": "x"}, ctx))
    assert "error" in bad, f"未知 action 必须报错: {bad}"

    closed = asyncio.run(
        CloseHumanReviewTool().execute({"summary": "评审已通过", "target_role": "owner"}, ctx)
    )
    assert closed.get("ok") is True, f"close_human_review 必须 ok: {closed}"
    owner_inbox = comms.read_inbox("owner", include_seen=True)
    assert any("评审已通过" in m.body for m in owner_inbox), "评审关闭通知必须落盘"


def _ownership_wi() -> SimpleNamespace:
    return SimpleNamespace(
        id="wi-1",
        title="t",
        description="d",
        assignee_role="coordinator",
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"allowed_collaboration_targets": ["manager"]},
        },
    )


@pytest.mark.parametrize(
    "tool_name,args,expect_blocked",
    [
        pytest.param(
            "route_work",
            {"action": "send_followup", "target_role": "stranger"},
            True,
            id="route_work-illegal",
        ),
        pytest.param(
            "close_human_review",
            {"summary": "s", "target_role": "stranger"},
            True,
            id="close_review-illegal",
        ),
        pytest.param(
            "route_work",
            {"action": "send_followup", "target_role": "manager"},
            False,
            id="route_work-allowed",
        ),
        pytest.param(
            "close_human_review",
            {"summary": "s", "target_role": "manager"},
            False,
            id="close_review-allowed",
        ),
    ],
)
def test_contract_route_work_topology_guard(tool_name, args, expect_blocked) -> None:
    """契约 10（族级 parametrize=4）：route_work/close_human_review 的 target_role
    收件人必须过协作拓扑守卫——company_mode + ownership_contract 下，收件人不在
    allowed_collaboration_targets 时 ownership_guard_violation 必须拦截
    """
    from bandi.company.collaboration_policy import ownership_guard_violation

    violation = ownership_guard_violation(
        work_item=_ownership_wi(), tool_name=tool_name, arguments=args
    )
    if expect_blocked:
        assert violation is not None, f"{tool_name} 拓扑外收件人必须被拦截: {args}"
        assert "not allowed" in violation, f"拦截原因必须含 not allowed: {violation}"
    else:
        assert violation is None, f"{tool_name} 拓扑内收件人必须放行: {violation}"


def test_contract_close_review_default_target_guarded() -> None:
    """契约 close_human_review 缺省 target_role=owner 也必须过拓扑守卫——
    默认收件人不在 allowed 时同样拦截（fail-closed 不因缺省参数绕过）。"""
    from bandi.company.collaboration_policy import ownership_guard_violation

    violation = ownership_guard_violation(
        work_item=_ownership_wi(),
        tool_name="close_human_review",
        arguments={"summary": "s"},
    )
    assert violation is not None, "close_human_review 默认 owner 不在 allowed 时必须拦截"
    assert "not allowed" in violation


_MW_GUARD_CASES = [
    pytest.param(
        "route_work",
        {"action": "send_followup", "target_role": "stranger"},
        True,
        id="mw-route_work-illegal",
    ),
    pytest.param(
        "route_work",
        {"action": "send_followup", "target_role": "manager"},
        False,
        id="mw-route_work-allowed",
    ),
    pytest.param(
        "close_human_review",
        {"summary": "s", "target_role": "stranger"},
        True,
        id="mw-close_review-illegal",
    ),
    pytest.param(
        "close_human_review",
        {"summary": "s", "target_role": "manager"},
        False,
        id="mw-close_review-allowed",
    ),
    pytest.param(
        "close_human_review",
        {"summary": "s"},
        True,
        id="mw-close_review-default_owner",
    ),
]


@pytest.mark.parametrize("tool_name,args,expect_blocked", _MW_GUARD_CASES)
def test_contract_middleware_production_path_guards_collab(tool_name, args, expect_blocked) -> None:
    """契约 11（族级 parametrize=5）：拓扑守卫必须经 OwnershipMiddleware 生产路径
    （ownership_mw.py:61，agent_runner.py:553 enable_ownership=work_item is not None）
    生产调用路径族级覆盖）。route_work/close_human_review 拓扑外收件人 + 缺省
    owner 必须被 middleware 拦截，拓扑内放行。"""
    from bandi.core.message import ToolCall
    from bandi.tools.middleware.ownership_mw import OwnershipMiddleware
    from bandi.tools.pipeline import ToolContext

    mw = OwnershipMiddleware()
    ctx = ToolContext(extra={"work_item": _ownership_wi()})
    result = asyncio.run(
        mw.before_execute(ctx, ToolCall(id="call-1", name=tool_name, arguments=args))
    )
    if expect_blocked:
        assert not result.allow, f"{tool_name} 拓扑外收件人必须被 middleware 拦截: {args}"
        assert "not allowed" in (result.reason or ""), (
            f"拦截原因必须含 not allowed: {result.reason}"
        )
    else:
        assert result.allow, f"{tool_name} 拓扑内收件人必须被 middleware 放行: {result.reason}"


_COLLAB_WRITE_CASES = [
    pytest.param(SendDmTool, {"to_agent": "manager", "subject": "s", "body": "b"}, id="send_dm"),
    pytest.param(
        AskPeerAndWaitTool,
        {"to_agent": "manager", "subject": "s", "body": "b"},
        id="ask_peer_and_wait",
    ),
    pytest.param(ReplyMessageTool, {"message_id": "id", "body": "b"}, id="reply_message"),
    pytest.param(
        BroadcastIssueTool, {"to_agents": ["a"], "subject": "s", "body": "b"}, id="broadcast_issue"
    ),
    pytest.param(
        StartMeetingTool, {"topic": "t", "participants": ["a"], "agenda": "g"}, id="start_meeting"
    ),
    pytest.param(RespondMeetingTool, {"room_id": "r", "content": "c"}, id="respond_meeting"),
    pytest.param(
        RouteWorkTool, {"action": "send_followup", "target_role": "worker"}, id="route_work"
    ),
    pytest.param(CloseHumanReviewTool, {"summary": "s"}, id="close_human_review"),
]


@pytest.mark.parametrize("tool_cls,_params", _COLLAB_WRITE_CASES)
def test_contract_collab_write_declares_non_retryable(tool_cls, _params) -> None:
    """契约 12（族级 parametrize=8）：8 个协作写工具必须显式声明 retryable=False。

    现状核对：collaboration.py 全部工具只靠「失败返回 dict 而非抛异常」的隐式兜底
    （except → _err dict → executor:516 标 non_retryable）——一旦未来把某工具改成
    raise 走抛异常路径，立即落入默认重试 → 重复发消息/重复路由（定时炸弹）。
    显式声明后，成为权威，失败形态变化也不会重试。"""
    from bandi.tools.base import RetryPolicy

    tool = tool_cls()
    policy = getattr(tool, "retry_policy", None)
    assert policy is not None, f"{tool.name} 未声明 retry_policy——安全仅靠隐式 dict 兜底"
    assert isinstance(policy, RetryPolicy) and policy.retryable is False, (
        f"{tool.name} 写工具失败必须显式禁重试（防重复发消息/路由）"
    )


def test_contract_inbox_declares_read_only() -> None:
    """inbox 是读工具（只查收件箱不改变状态）——标 is_read_only=True
    超时走安全重试语义、同轮同参可去重。"""
    assert InboxTool().is_read_only is True


@pytest.mark.parametrize("tool_cls,_params", _COLLAB_WRITE_CASES)
def test_contract_collab_write_raise_never_retried(tool_cls, _params, monkeypatch) -> None:
    """契约 13（族级 parametrize=8，破坏者视角）：协作写工具即使失败形态从
    「返回 dict」改成「抛普通异常」（未来重构路径），pipeline 也绝不得重试——
    显式 retryable=False 是 权威；execute 计数必须 ==1。

    用 monkeypatch 把 execute 替换为 raise OSError 模拟改版，走真实
    ToolExecutor + RetryMiddleware 生产链路断言最终产物（执行次数）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.middleware.retry_mw import RetryMiddleware
    from bandi.tools.pipeline import ToolPipeline
    from bandi.tools.registry import ToolRegistry

    calls: list[int] = [0]

    async def _boom(self, params: dict, context=None) -> str:  # type: ignore[no-untyped-def]
        calls[0] += 1
        raise OSError("peer transport socket unavailable")

    monkeypatch.setattr(tool_cls, "execute", _boom)

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.0))
    registry = ToolRegistry()
    registry.register_instance(tool_cls())
    executor = ToolExecutor(registry, tool_pipeline=pipeline)
    results = asyncio.run(
        executor.execute([ToolCall(id="call-1", name=tool_cls.name, arguments=_params)])
    )
    assert results[0].success is False
    assert calls == [1], f"{tool_cls.name} 失败被重试 {calls[0] - 1} 次——写工具抛异常也必须零重试"
