# 测试重点：binding 构建 / handler 委托 / fail-closed / 参数标准化
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.tools.collaboration_dispatch import (
    BOUND_HANDLERS,
    HANDLERS,
    CollaborationRuntimeBinding,
    CollaborationRuntimeContext,
    _env,
    _parse_allowed_tools,
    allowed_tool_names,
    build_collaboration_runtime_from_env,
    dispatch_collaboration_tool,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


def _comms(tmp_path: Path) -> Any:
    from bandi.company.communication import CommunicationManager

    return CommunicationManager(workspace=str(tmp_path), project_id="p1", session_id="s1")


def _make_binding(
    tmp_path: Path,
    role: str = "worker",
) -> CollaborationRuntimeBinding:
    comms = _comms(tmp_path)
    ctx = CollaborationRuntimeContext(
        workspace=str(tmp_path),
        project_id="p1",
        session_id="s1",
        role_id=role,
    )
    return CollaborationRuntimeBinding(
        comms=comms, ctx=ctx, allowed_tools=set(BOUND_HANDLERS.keys())
    )


def _make_env(tmp_path: Path, role: str = "worker") -> dict[str, str]:
    return {
        "BANDI_WORKSPACE_ROOT": str(tmp_path),
        "BANDI_COMMS_PROJECT": "p1",
        "BANDI_COMMS_SESSION": "s1",
        "BANDI_COMMS_FROM": role,
    }


# ── env 辅助 ──────────────────────────────────────────────────────────────────


class TestEnvHelpers:
    def test_env_default(self) -> None:
        assert _env("NONEXISTENT_KEY_XYZ", default="fallback") == "fallback"

    def test_env_reads_from_mapping(self) -> None:
        env = {"KEY_A": "val_a"}
        assert _env("KEY_A", env=env) == "val_a"
        assert _env("MISSING", default="d", env=env) == "d"

    def test_parse_allowed_tools_json(self) -> None:
        result = _parse_allowed_tools('["inbox", "send_dm"]')
        assert result == {"inbox", "send_dm"}

    def test_parse_allowed_tools_csv(self) -> None:
        result = _parse_allowed_tools("inbox, send_dm, reply_message")
        assert result == {"inbox", "send_dm", "reply_message"}

    def test_parse_allowed_tools_empty(self) -> None:
        assert _parse_allowed_tools("") == set()
        assert _parse_allowed_tools(None) == set()

    def test_parse_allowed_tools_invalid_json_splits_by_commas(self) -> None:
        result = _parse_allowed_tools("not,json,at,all")
        assert result == {"not", "json", "at", "all"}


class TestAllowedToolNames:
    def test_explicit_env_overrides(self) -> None:
        env = {"BANDI_COMMS_FROM": "worker", "BANDI_ALLOWED_COLLAB_TOOLS": '["inbox"]'}
        result = allowed_tool_names(
            _task=None,
            _context=SimpleNamespace(role_id="worker", task=None),
            _manager=SimpleNamespace(),
            env=env,
        )
        assert result == {"inbox"}


# ── binding 构建 ──────────────────────────────────────────────────────────────


class TestBuildRuntimeFromEnv:
    @pytest.mark.asyncio
    async def test_build_with_env_vars(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        svc, ctx, store, comms, _org = await build_collaboration_runtime_from_env(env)
        assert ctx.role_id == "worker"
        assert ctx.project_id == "p1"
        assert ctx.session_id == "s1"
        assert comms is not None

    @pytest.mark.asyncio
    async def test_build_without_from_role_defaults_empty(self, tmp_path: Path) -> None:
        env = {
            "BANDI_WORKSPACE_ROOT": str(tmp_path),
            "BANDI_COMMS_PROJECT": "p2",
            "BANDI_COMMS_SESSION": "s2",
        }
        _, ctx, _, comms, _org = await build_collaboration_runtime_from_env(env)
        assert ctx.role_id == ""
        assert ctx.project_id == "p2"
        assert comms is not None


# ── bound handler 委托 ────────────────────────────────────────────────────────


class TestBoundHandlers:
    @pytest.mark.asyncio
    async def test_inbox_returns_unread_count(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        binding.comms.send_dm("other", "worker", "提醒", "下午开会")
        result = await BOUND_HANDLERS["inbox"]({"action": "status"}, binding)
        assert isinstance(result, dict)
        assert result.get("unread_count", 0) >= 0

    @pytest.mark.asyncio
    async def test_inbox_fails_without_role(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path, role="")
        result = await BOUND_HANDLERS["inbox"]({"action": "status"}, binding)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_send_dm_persists(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        result = await BOUND_HANDLERS["send_dm"](
            {"to_agent": "mgr", "subject": "任务", "body": "完成了"}, binding
        )
        assert "error" not in result
        msgs = binding.comms.read_inbox("mgr", include_seen=True)
        assert len(msgs) >= 1
        assert msgs[-1].sender_role == "worker"

    @pytest.mark.asyncio
    async def test_send_dm_fails_without_role(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path, role="")
        result = await BOUND_HANDLERS["send_dm"](
            {"to_agent": "m", "subject": "s", "body": "b"}, binding
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reply_message_links(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        orig_id = binding.comms.send_dm("mgr", "worker", "Q", "请回复")
        result = await BOUND_HANDLERS["reply_message"](
            {"message_id": orig_id, "body": "好的"}, binding
        )
        assert "error" not in result
        replies = binding.comms.read_inbox("mgr", include_seen=True)
        assert any("好的" in r.body for r in replies)

    @pytest.mark.asyncio
    async def test_broadcast_issue_sends_to_all(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        result = await BOUND_HANDLERS["broadcast_issue"](
            {"to_agents": ["a", "b"], "subject": "公告", "body": "紧急"}, binding
        )
        assert "error" not in result
        for role in ("a", "b"):
            msgs = binding.comms.read_inbox(role, include_seen=True)
            assert any("紧急" in m.body for m in msgs)

    @pytest.mark.asyncio
    async def test_start_meeting_creates_room(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        result = await BOUND_HANDLERS["start_meeting"](
            {"topic": "周会", "participants": ["x", "y"], "agenda": "讨论进度"},
            binding,
        )
        assert "error" not in result
        assert "room_id" in result
        room = binding.comms.get_room(result["room_id"])
        assert room is not None
        assert "x" in room.participants and "y" in room.participants

    @pytest.mark.asyncio
    async def test_respond_meeting_append(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        create_result = await BOUND_HANDLERS["start_meeting"](
            {"topic": "会", "participants": ["a"], "agenda": "议程"}, binding
        )
        room_id = create_result["room_id"]
        result = await BOUND_HANDLERS["respond_meeting"](
            {"room_id": room_id, "content": "同意方案"}, binding
        )
        assert "error" not in result
        room = binding.comms.get_room(room_id)
        assert room is not None
        assert any("同意方案" in m.content for m in room.messages)

    @pytest.mark.asyncio
    async def test_route_work_send_followup(self, tmp_path: Path) -> None:
        binding = _make_binding(tmp_path)
        result = await BOUND_HANDLERS["route_work"](
            {"action": "send_followup", "target_role": "dev", "prompt": "跟进一下"},
            binding,
        )
        assert "error" not in result
        assert result.get("action") == "send_followup"

    @pytest.mark.asyncio
    async def test_close_human_review_not_dispatchable(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        result, is_error = await dispatch_collaboration_tool(
            "close_human_review",
            {
                "summary": "评审通过",
                "target_role": "owner",
                "human_review_close_allowed": True,
            },
            env=env,
        )
        assert is_error, result
        assert "unknown tool" in str(result.get("error", "")).lower()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, tmp_path: Path) -> None:
        result = BOUND_HANDLERS.get("nonexistent_tool", lambda _a, _b: {"error": "unknown"})(
            {}, _make_binding(tmp_path)
        )
        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_hand_off_all_handlers_present(self) -> None:
        expected = {
            "inbox",
            "send_dm",
            "ask_peer_and_wait",
            "reply_message",
            "broadcast_issue",
            "start_meeting",
            "respond_meeting",
            "route_work",
        }
        assert set(BOUND_HANDLERS.keys()) == expected
        assert set(HANDLERS.keys()) == expected


# ── env_handler 包装 ───────────────────────────────────────────────────────────


class TestEnvHandler:
    @pytest.mark.asyncio
    async def test_env_handler_calls_build_runtime(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        result = await HANDLERS["send_dm"](
            {"to_agent": "mgr", "subject": "测试", "body": "hello"},
            env,
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_env_handler_missing_role_fails_closed(self, tmp_path: Path) -> None:
        env = {
            "BANDI_WORKSPACE_ROOT": str(tmp_path),
            "BANDI_COMMS_PROJECT": "p1",
            "BANDI_COMMS_SESSION": "s1",
        }
        result = await HANDLERS["send_dm"](
            {"to_agent": "m", "subject": "s", "body": "b"},
            env,
        )
        assert "error" in result


# ── 端到端：dispatch_collaboration_tool ────────────────────────────────────────


class TestDispatchCollaborationTool:
    @pytest.mark.asyncio
    async def test_roundtrip_send_and_read(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        dispatch_result, is_error = await dispatch_collaboration_tool(
            "send_dm",
            {"to_agent": "boss", "subject": "报告", "body": "今日完成"},
            env=env,
        )
        assert not is_error
        assert dispatch_result.get("to_agent") == "boss"

    @pytest.mark.asyncio
    async def test_inbox_returns_count_after_send(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        await dispatch_collaboration_tool(
            "send_dm", {"to_agent": "worker", "subject": "新消息", "body": "内容"}, env=env
        )
        dispatch_result, is_error = await dispatch_collaboration_tool(
            "inbox", {"action": "status"}, env=env
        )
        assert not is_error
        assert dispatch_result.get("unread_count", 0) >= 1

    @pytest.mark.asyncio
    async def test_unknown_tool_is_error(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        result, is_error = await dispatch_collaboration_tool("no_such_tool", {}, env=env)
        assert is_error
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_env_fails_closed(self) -> None:
        result, is_error = await dispatch_collaboration_tool(
            "send_dm", {"to_agent": "m", "subject": "s", "body": "b"}
        )
        assert is_error
        assert "error" in result

    @pytest.mark.asyncio
    async def test_allowed_tools_enforced_by_env(self) -> None:
        """allowed_tools 白名单必须被强制执行——空名单时所有工具均被拒。"""
        env = _make_env(Path("."))
        env["BANDI_ALLOWED_COLLAB_TOOLS"] = "[]"
        result, is_error = await dispatch_collaboration_tool(
            "send_dm", {"to_agent": "m", "subject": "s", "body": "b"}, env=env
        )
        assert is_error is True
        assert "error" in result
        err_lower = result["error"].lower()
        assert "not allowed" in err_lower or "not available" in err_lower

    @pytest.mark.asyncio
    async def test_allowed_tools_partial_enforce(self) -> None:
        """部分白名单：不在白名单的工具被拒，在白名单的工具正常执行。"""
        env = _make_env(Path("."))
        env["BANDI_ALLOWED_COLLAB_TOOLS"] = '["inbox"]'
        result, is_error = await dispatch_collaboration_tool("inbox", {"action": "status"}, env=env)
        assert is_error is False
        # send_dm not allowed → should be rejected
        result2, is_error2 = await dispatch_collaboration_tool(
            "send_dm", {"to_agent": "m", "subject": "s", "body": "b"}, env=env
        )
        assert is_error2 is True
        assert "error" in result2
