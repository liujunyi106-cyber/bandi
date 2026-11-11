from __future__ import annotations

from dataclasses import dataclass

import pytest

from bandi.company.group_goal import (
    GoalStatus,
    GroupGoalError,
    GroupGoalService,
    TaskStatus,
)
from bandi.company.group_goal_registry import set_group_goal_service
from bandi.company.group_goal_store import GroupGoalStore
from bandi.company.group_round import GroupRound, RoundSlot, render_round_context
from bandi.company.org_engine import OrgEngine
from bandi.tools.base import ToolPermissionContext
from bandi.tools.group_goal import GroupGoalTool


@dataclass
class _Round:
    member_role_ids: list[str]


class _Rounds:
    def __init__(self, members: list[str]) -> None:
        self.members = members

    def get_active_by_session(self, session_id: str) -> _Round:
        return _Round(self.members)

    def list_by_session(self, session_id: str, limit: int = 20) -> list[_Round]:
        return [_Round(self.members)]


@pytest.fixture()
def service(tmp_path):
    org = OrgEngine()
    org.register_role("lead", "组长", reports_to=None, is_final_decider=True)
    org.register_role("designer", "产品设计师", reports_to="lead")
    org.register_role("frontend", "前端代码员工", reports_to="lead")
    store = GroupGoalStore(tmp_path / "goals.db")
    return GroupGoalService(
        store,
        org,
        round_store=_Rounds(["lead", "designer", "frontend"]),
    )


def _start(service: GroupGoalService) -> None:
    service.draft(
        session_id="s1",
        project_id="p1",
        brief="做一个目标账本",
        title="目标账本",
        role_id="lead",
        member_role_ids=["lead", "designer", "frontend"],
    )
    service.start(
        session_id="s1",
        role_id="lead",
        objective="群聊每轮共享同一个目标",
        acceptance="目标条可见且员工只看到当前任务",
        constraints=["不要提前进入开发"],
        stages=[
            {
                "title": "目标条",
                "acceptance": "目标条可见",
                "tasks": [
                    {"bot": "产品设计师", "task": "定义信息层级"},
                    {"bot": "前端代码员工", "task": "实现组件"},
                ],
            },
            {
                "title": "联调",
                "acceptance": "三轮对话不漂移",
                "tasks": [{"bot": "产品设计师", "task": "跑长对话验收"}],
            },
        ],
        title="目标账本",
        member_role_ids=["lead", "designer", "frontend"],
    )


def test_goal_lifecycle_and_task_status(service):
    _start(service)
    service.mark_working("s1", "designer")
    service.mark_reported("s1", "designer")
    goal = service.get("s1")
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert goal.current == 0
    assert goal.stages[0].tasks[0].status == TaskStatus.REPORTED

    service.pass_stage(session_id="s1", role_id="lead", evidence="目标条已渲染")
    goal = service.get("s1")
    assert goal is not None
    assert goal.current == 1
    assert goal.stages[0].status.value == "passed"
    assert goal.stages[1].status.value == "active"

    service.pass_stage(session_id="s1", role_id="lead", evidence="三轮对话通过")
    service.complete(
        session_id="s1",
        role_id="lead",
        evidence="验收记录",
        summary="目标账本可用",
    )
    goal = service.get("s1")
    assert goal is not None
    assert goal.status == GoalStatus.COMPLETED


def test_start_without_draft_creates_active_goal(service):
    service.start(
        session_id="s2",
        role_id="lead",
        objective="直接从对话开始",
        acceptance="目标条出现",
        constraints=[],
        stages=[
            {
                "title": "开始",
                "acceptance": "可见",
                "tasks": [{"bot": "产品设计师", "task": "确认目标"}],
            }
        ],
        title="直接目标",
        member_role_ids=["lead", "designer"],
    )
    goal = service.get("s2")
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE


def test_only_owner_changes_goal(service):
    _start(service)
    with pytest.raises(GroupGoalError, match="顶层角色"):
        service.pass_stage(session_id="s1", role_id="designer", evidence="我想通过")
    assert service.render_ledger(service.get("s1"), "designer").startswith("目标「目标账本」")


def test_prompt_block_owner_and_member(service):
    _start(service)
    owner_block = service.render_block("s1", "lead")
    member_block = service.render_block("s1", "designer")
    assert "<group_goal" in owner_block
    assert "Objective" in owner_block
    assert "你的任务" in member_block
    assert "定义信息层级" in member_block
    assert "前端代码员工" not in member_block


def test_rework_resets_current_stage_tasks(service):
    _start(service)
    service.mark_working("s1", "designer")
    service.rework(
        session_id="s1",
        role_id="lead",
        reason="信息层级不完整",
        target_role_id="designer",
        member_role_ids=["lead", "designer", "frontend"],
    )
    goal = service.get("s1")
    assert goal is not None
    assert goal.stages[0].tasks[0].status == TaskStatus.PENDING
    assert goal.stages[0].reworks == 1
    assert "信息层级不完整" in goal.stages[0].feedback


def test_round_context_uses_goal_block_and_drops_soft_hint():
    round_ = GroupRound(
        round_id="r1",
        session_id="s1",
        project_id="p1",
        user_input="只讨论，不写文件",
        member_role_ids=["lead", "designer"],
        slots=[RoundSlot("slot-1", "designer")],
    )
    text = render_round_context(
        round_,
        round_.slots[0],
        goal_context='<group_goal status="active">目标</group_goal>',
    )
    assert '<group_goal status="active">目标</group_goal>' in text
    assert "目标约束" not in text


@pytest.mark.asyncio
async def test_tool_forwards_session_and_role():
    class _Service:
        def __init__(self) -> None:
            self.calls = []

        def apply_action(self, session_id, role_id, action, payload):
            self.calls.append((session_id, role_id, action, payload))
            return {"message": "ok", "goal": None}

    service = _Service()
    set_group_goal_service(service)
    try:
        context = ToolPermissionContext(
            role="lead",
            extra={"session_id": "s1", "project_id": "p1"},
        )
        result = await GroupGoalTool().execute({"action": "show"}, context)
        assert result["success"] is True
        assert service.calls[0][0:3] == ("s1", "lead", "show")
    finally:
        set_group_goal_service(None)
