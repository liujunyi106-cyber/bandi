# 边界/异常覆盖：缺参、非法参数、无 comms、房间不存在、回复未找到。
from __future__ import annotations

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


class TestSendDm:
    @pytest.mark.asyncio
    async def test_missing_required(self, tmp_path) -> None:
        ctx = _ctx(_comms(tmp_path))
        assert "error" in await SendDmTool().execute({"to_agent": "m"}, ctx)
        assert "error" in await SendDmTool().execute({"subject": "s", "body": "b"}, ctx)

    @pytest.mark.asyncio
    async def test_no_comms(self) -> None:
        ctx = ToolPermissionContext(role="worker", communication=None)
        result = await SendDmTool().execute({"to_agent": "m", "subject": "s", "body": "b"}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_sender_is_context_role(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        await SendDmTool().execute(
            {"to_agent": "m", "subject": "s", "body": "b"}, _ctx(comms, role="lead")
        )
        msg = comms.read_inbox("m", include_seen=True)[-1]
        assert msg.sender_role == "lead"


class TestInbox:
    @pytest.mark.asyncio
    async def test_empty_inbox(self, tmp_path) -> None:
        result = await InboxTool().execute({"action": "status"}, _ctx(_comms(tmp_path)))
        assert result["unread_count"] == 0
        assert result["messages"] == []

    @pytest.mark.asyncio
    async def test_unknown_action_defaults_status(self, tmp_path) -> None:
        result = await InboxTool().execute({"action": "bogus"}, _ctx(_comms(tmp_path)))
        assert result["action"] == "status"

    @pytest.mark.asyncio
    async def test_limit_truncates(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        for i in range(5):
            comms.send_dm("m", "worker", f"sub{i}", "body")
        result = await InboxTool().execute({"action": "peek", "limit": 2}, _ctx(comms))
        assert result["unread_count"] == 2
        assert len(result["messages"]) == 2


class TestAskPeerAndWait:
    @pytest.mark.asyncio
    async def test_reply_detected(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        ctx = _ctx(comms)
        result = await AskPeerAndWaitTool().execute(
            {"to_agent": "m", "subject": "q", "body": "在吗", "timeout_seconds": 5}, ctx
        )
        comms.send_dm("m", "worker", "re", "在的", blocking=False, reply_to_msg_id=result["msg_id"])
        replies = [
            msg
            for msg in comms.read_inbox("worker", include_seen=True)
            if msg.metadata.get("reply_to_msg_id") == result["msg_id"]
        ]
        assert replies and "在的" in replies[-1].body

    @pytest.mark.asyncio
    @pytest.mark.heavy
    async def test_timeout_floor_one(self, tmp_path) -> None:
        result = await AskPeerAndWaitTool().execute(
            {"to_agent": "m", "subject": "q", "body": "b", "timeout_seconds": 0},
            _ctx(_comms(tmp_path)),
        )
        assert result["reply"] is None


class TestReplyMessage:
    @pytest.mark.asyncio
    async def test_unknown_message_id(self, tmp_path) -> None:
        result = await ReplyMessageTool().execute(
            {"message_id": "nope", "body": "hi"}, _ctx(_comms(tmp_path))
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_body(self, tmp_path) -> None:
        result = await ReplyMessageTool().execute({"message_id": "x"}, _ctx(_comms(tmp_path)))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reply_delivered_to_original_sender(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        original_id = comms.send_dm("manager", "worker", "问题", "请确认", blocking=False)
        await ReplyMessageTool().execute({"message_id": original_id, "body": "已确认"}, _ctx(comms))
        manager_inbox = comms.read_inbox("manager", include_seen=True)
        assert any("已确认" in m.body for m in manager_inbox), "回复必须落到原发送者收件箱"


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_empty_recipients(self, tmp_path) -> None:
        result = await BroadcastIssueTool().execute(
            {"to_agents": [], "subject": "s", "body": "b"}, _ctx(_comms(tmp_path))
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_sent_ids_match_recipients(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await BroadcastIssueTool().execute(
            {"to_agents": ["a", "b"], "subject": "s", "body": "b"}, _ctx(comms, role="m")
        )
        assert len(result["sent"]) == 2

    @pytest.mark.asyncio
    async def test_sender_is_context_role(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        await BroadcastIssueTool().execute(
            {"to_agents": ["a"], "subject": "公告", "body": "b"}, _ctx(comms, role="lead")
        )
        inbox = comms.read_inbox("a", include_seen=True)
        assert inbox[-1].sender_role == "lead", "广播 sender 必须取 context.role"


class TestMeeting:
    @pytest.mark.asyncio
    async def test_start_requires_participants(self, tmp_path) -> None:
        result = await StartMeetingTool().execute(
            {"topic": "t", "participants": [], "agenda": "a"}, _ctx(_comms(tmp_path))
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_respond_missing_room(self, tmp_path) -> None:
        result = await RespondMeetingTool().execute(
            {"room_id": "nope", "content": "hi"}, _ctx(_comms(tmp_path))
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_meeting_invites_participants(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        await StartMeetingTool().execute(
            {"topic": "评审", "participants": ["a", "b"], "agenda": "讨论"}, _ctx(comms, role="m")
        )
        for role in ("a", "b"):
            inbox = comms.read_inbox(role, include_seen=True)
            assert any("评审" in m.subject for m in inbox), f"{role} 必须收到会议邀请"


class TestRouteWork:
    @pytest.mark.asyncio
    async def test_spawn_task_high_urgency(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await RouteWorkTool().execute(
            {"action": "spawn_task", "target_role": "w", "prompt": "干活", "priority": "high"},
            _ctx(comms, role="coordinator"),
        )
        assert result["action"] == "spawn_task"
        assert result["priority"] == "high"

    @pytest.mark.asyncio
    async def test_escalate_blocking(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await RouteWorkTool().execute(
            {"action": "escalate", "target_role": "manager", "prompt": "升级"}, _ctx(comms)
        )
        assert result["routed_to"] == "manager"
        msg = comms.read_inbox("manager", include_seen=True)[-1]
        assert msg.blocking, "escalate 必须请求回复（blocking）"

    @pytest.mark.asyncio
    async def test_followup_not_blocking(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await RouteWorkTool().execute(
            {"action": "send_followup", "target_role": "w", "prompt": "跟进"},
            _ctx(comms, role="coordinator"),
        )
        assert result["routed_to"] == "w"
        msg = comms.read_inbox("w", include_seen=True)[-1]
        assert not msg.blocking, "send_followup 必须非阻塞（普通跟进，不请求回复）"


class TestCloseHumanReview:
    @pytest.mark.asyncio
    async def test_missing_summary(self, tmp_path) -> None:
        result = await CloseHumanReviewTool().execute({"summary": ""}, _ctx(_comms(tmp_path)))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_default_target_owner(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await CloseHumanReviewTool().execute(
            {"summary": "通过", "user_message": "无"}, _ctx(comms, role="worker", authorized=True)
        )
        assert result["ok"] is True
        assert result["closed_by"] == "worker"
        owner_inbox = comms.read_inbox("owner", include_seen=True)
        assert any("通过" in m.body for m in owner_inbox), "默认通知 owner 必须落盘"

    @pytest.mark.asyncio
    async def test_explicit_target_role(self, tmp_path) -> None:
        comms = _comms(tmp_path)
        result = await CloseHumanReviewTool().execute(
            {"summary": "通过", "target_role": "reviewer"},
            _ctx(comms, role="worker", authorized=True),
        )
        assert result["ok"] is True
        inbox = comms.read_inbox("reviewer", include_seen=True)
        assert any("通过" in m.body for m in inbox), "显式 target_role 必须收到通知"
