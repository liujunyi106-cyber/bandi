"""契约：协作/派单工具在角色 tool_tags 裁剪下始终可用（组织结构性权利）。

- 背景：自定义角色 tool_tags 不含 workflow 时，协作 9 工具 + spawn_thread_task +
  经理 3 工具（board_read/modify/delete）被裁 →「上级调不了下级」潜在路径
  （用户拍板：创建员工/选团队模板后必须能调用）。经理工具的越权风险由运行时
  所有权校验兜底（仅父卡负责人可操作），不靠 schema 面收窄。
- 豁免面：schema 过滤（factory.filter_tools_by_role_tags）与执行门禁
  （executor._role_gate）两层一致；单一来源 ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS。
- 破坏者：豁免不得外溢到同类别（workflow）非协作工具（team_create 仍按 tags 裁）；
  fail-closed 空名单（[] = 显式全禁）语义保持；无限制角色（None）原样放行。
"""

from __future__ import annotations

from bandi.agent.factory import filter_tools_by_role_tags
from bandi.core.message import ToolCall
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
from bandi.tools.executor import ToolExecutor
from bandi.tools.group_goal import GroupGoalTool
from bandi.tools.manager_board import (
    DeleteWorkItemTool,
    ManagerBoardReadTool,
    ModifyWorkItemTool,
)
from bandi.tools.registry import ToolRegistry
from bandi.tools.team import TeamCreateTool
from bandi.tools.thread_task import SpawnThreadTaskTool

COLLAB_ALWAYS = frozenset(
    {
        "inbox",
        "send_dm",
        "ask_peer_and_wait",
        "reply_message",
        "broadcast_issue",
        "start_meeting",
        "respond_meeting",
        "route_work",
        "close_human_review",
        "spawn_thread_task",
        "group_goal",
        "manager_board_read",
        "modify_work_item",
        "delete_work_item",
    }
)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    for cls in (
        InboxTool,
        SendDmTool,
        AskPeerAndWaitTool,
        ReplyMessageTool,
        BroadcastIssueTool,
        StartMeetingTool,
        RespondMeetingTool,
        RouteWorkTool,
        CloseHumanReviewTool,
        SpawnThreadTaskTool,
        GroupGoalTool,
        ManagerBoardReadTool,
        ModifyWorkItemTool,
        DeleteWorkItemTool,
        TeamCreateTool,
    ):
        reg.register(cls)
    return reg


class TestCollabToolsSurviveRoleFilter:
    def test_constant_is_single_source_of_collab_family(self) -> None:
        assert ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS == COLLAB_ALWAYS

    def test_schema_filter_keeps_all_collab_tools(self) -> None:
        reg = _registry()
        names = [t.name for t in reg.list_tools()]
        filtered = set(filter_tools_by_role_tags(names, reg, ["web"]))
        missing = COLLAB_ALWAYS - filtered
        assert not missing, f"协作工具被角色裁剪误伤：{sorted(missing)}"

    def test_exemption_does_not_leak_to_non_collab_workflow_tools(self) -> None:
        reg = _registry()
        names = [t.name for t in reg.list_tools()]
        filtered = set(filter_tools_by_role_tags(names, reg, ["web"]))
        assert "team_create" not in filtered, (
            "豁免仅限协作清单——同类别（workflow）非协作工具仍须按 tags 裁剪"
        )

    def test_empty_role_tags_still_fail_closed(self) -> None:
        reg = _registry()
        names = [t.name for t in reg.list_tools()]
        assert filter_tools_by_role_tags(names, reg, []) == [], (
            "空名单=显式全禁（fail-closed）语义不得因协作豁免而放宽"
        )

    def test_unrestricted_role_passes_through(self) -> None:
        reg = _registry()
        names = [t.name for t in reg.list_tools()]
        assert filter_tools_by_role_tags(names, reg, None) == names


class TestExecutorGateCollab:
    def test_gate_allows_collab_and_denies_others(self) -> None:
        reg = _registry()
        guard = ToolExecutor(reg, role_tool_tags=["web"])
        assert guard._role_gate(ToolCall(id="t1", name="send_dm", arguments={})) is None, (
            "协作工具在执行门禁必须放行（schema 面豁免的执行面一致）"
        )
        assert (
            guard._role_gate(ToolCall(id="t2", name="spawn_thread_task", arguments={})) is None
        ), "派单工具在执行门禁必须放行"
        for manager_tool in ("manager_board_read", "modify_work_item", "delete_work_item"):
            assert (
                guard._role_gate(ToolCall(id=f"t-{manager_tool}", name=manager_tool, arguments={}))
                is None
            ), f"经理工具 {manager_tool} 在执行门禁必须放行"
        denied = guard._role_gate(ToolCall(id="t3", name="team_create", arguments={}))
        assert denied is not None and denied.success is False, (
            "非协作 workflow 工具在受限角色下仍须被拒"
        )
