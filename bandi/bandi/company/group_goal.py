# 功能：群聊目标账本——目标/验收/约束/阶段/成员任务与状态机
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bandi.core.exceptions import BandiError

TITLE_MAX = 80
BRIEF_MAX = 2000
TEXT_MAX = 2000
RULE_MAX = 300
RULES_MAX = 20
STAGES_MAX = 20
STAGE_TITLE_MAX = 80
STAGE_ACCEPTANCE_MAX = 1000
TASKS_MAX = 12
TASK_MAX = 500
NOTE_MAX = 300
REASON_MAX = 300
EVENTS_MAX = 50
PAST_MAX = 5


class GroupGoalError(BandiError):
    """功能：群聊目标操作被拒绝或参数不合法。"""


class GoalStatus(StrEnum):
    DRAFTING = "drafting"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"


class StageStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PASSED = "passed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    WORKING = "working"
    REPORTED = "reported"
    DONE = "done"


TASK_LABEL = {
    TaskStatus.PENDING: "未开始",
    TaskStatus.WORKING: "进行中",
    TaskStatus.REPORTED: "已回报",
    TaskStatus.DONE: "已完成",
}


@dataclass
class GoalTask:
    task_id: str
    role_id: str
    task: str
    status: TaskStatus = TaskStatus.PENDING
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role_id": self.role_id,
            "task": self.task,
            "status": self.status.value,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GoalTask:
        try:
            status = TaskStatus(str(payload.get("status") or TaskStatus.PENDING.value))
        except ValueError:
            status = TaskStatus.PENDING
        return cls(
            task_id=str(payload.get("task_id") or uuid.uuid4().hex),
            role_id=str(payload.get("role_id") or ""),
            task=str(payload.get("task") or ""),
            status=status,
            note=str(payload.get("note") or ""),
        )


@dataclass
class GoalStage:
    stage_id: str
    title: str
    acceptance: str
    tasks: list[GoalTask] = field(default_factory=list)
    status: StageStatus = StageStatus.PENDING
    evidence: str = ""
    reworks: int = 0
    feedback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "title": self.title,
            "acceptance": self.acceptance,
            "tasks": [task.to_dict() for task in self.tasks],
            "status": self.status.value,
            "evidence": self.evidence,
            "reworks": self.reworks,
            "feedback": self.feedback,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GoalStage:
        try:
            status = StageStatus(str(payload.get("status") or StageStatus.PENDING.value))
        except ValueError:
            status = StageStatus.PENDING
        return cls(
            stage_id=str(payload.get("stage_id") or uuid.uuid4().hex),
            title=str(payload.get("title") or ""),
            acceptance=str(payload.get("acceptance") or ""),
            tasks=[
                GoalTask.from_dict(item)
                for item in (payload.get("tasks") or [])
                if isinstance(item, dict)
            ],
            status=status,
            evidence=str(payload.get("evidence") or ""),
            reworks=int(payload.get("reworks") or 0),
            feedback=str(payload.get("feedback") or ""),
        )


@dataclass
class GroupGoal:
    goal_id: str
    session_id: str
    project_id: str = ""
    title: str = ""
    brief: str = ""
    objective: str = ""
    acceptance: str = ""
    constraints: list[str] = field(default_factory=list)
    stages: list[GoalStage] = field(default_factory=list)
    current: int = -1
    status: GoalStatus = GoalStatus.DRAFTING
    owner_role_id: str = ""
    member_role_ids: list[str] = field(default_factory=list)
    evidence: str = ""
    summary: str = ""
    reason: str = ""
    revision: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    past_goals: list[dict[str, Any]] = field(default_factory=list)
    stage_counter: int = 0
    task_counter: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def current_stage(self) -> GoalStage | None:
        if 0 <= self.current < len(self.stages):
            return self.stages[self.current]
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "title": self.title,
            "brief": self.brief,
            "objective": self.objective,
            "acceptance": self.acceptance,
            "constraints": list(self.constraints),
            "stages": [stage.to_dict() for stage in self.stages],
            "current": self.current,
            "status": self.status.value,
            "owner_role_id": self.owner_role_id,
            "member_role_ids": list(self.member_role_ids),
            "evidence": self.evidence,
            "summary": self.summary,
            "reason": self.reason,
            "revision": self.revision,
            "events": list(self.events),
            "past_goals": list(self.past_goals),
            "stage_counter": self.stage_counter,
            "task_counter": self.task_counter,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GroupGoal:
        try:
            status = GoalStatus(str(payload.get("status") or GoalStatus.DRAFTING.value))
        except ValueError:
            status = GoalStatus.DRAFTING
        return cls(
            goal_id=str(payload.get("goal_id") or uuid.uuid4().hex),
            session_id=str(payload.get("session_id") or ""),
            project_id=str(payload.get("project_id") or ""),
            title=str(payload.get("title") or ""),
            brief=str(payload.get("brief") or ""),
            objective=str(payload.get("objective") or ""),
            acceptance=str(payload.get("acceptance") or ""),
            constraints=[str(item) for item in (payload.get("constraints") or [])],
            stages=[
                GoalStage.from_dict(item)
                for item in (payload.get("stages") or [])
                if isinstance(item, dict)
            ],
            current=(int(payload["current"]) if payload.get("current") is not None else -1),
            status=status,
            owner_role_id=str(payload.get("owner_role_id") or ""),
            member_role_ids=[str(item) for item in (payload.get("member_role_ids") or [])],
            evidence=str(payload.get("evidence") or ""),
            summary=str(payload.get("summary") or ""),
            reason=str(payload.get("reason") or ""),
            revision=int(payload.get("revision") or 0),
            events=[dict(item) for item in (payload.get("events") or []) if isinstance(item, dict)],
            past_goals=[
                dict(item) for item in (payload.get("past_goals") or []) if isinstance(item, dict)
            ],
            stage_counter=int(payload.get("stage_counter") or 0),
            task_counter=int(payload.get("task_counter") or 0),
            created_at=float(payload.get("created_at") or time.time()),
            updated_at=float(payload.get("updated_at") or time.time()),
        )


def _clip(value: Any, limit: int, field_name: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise GroupGoalError(f"{field_name} 超过 {limit} 字")
    return text


def _require(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise GroupGoalError(f"{field_name} 不能为空")
    return text


def _clean_constraints(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise GroupGoalError("constraints 必须是字符串数组")
    if len(values) > RULES_MAX:
        raise GroupGoalError(f"constraints 最多 {RULES_MAX} 条")
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > RULE_MAX:
            raise GroupGoalError(f"单条 constraint 超过 {RULE_MAX} 字")
        result.append(text)
    return result


class GroupGoalService:
    """功能：群聊目标账本服务——顶层角色维护，成员只读当前阶段任务。"""

    def __init__(
        self,
        store: Any,
        org_engine: Any | None = None,
        *,
        round_store: Any | None = None,
        broadcast: Any | None = None,
    ) -> None:
        self._store = store
        self._org = org_engine
        self._rounds = round_store
        self._broadcast = broadcast

    def get(self, session_id: str) -> GroupGoal | None:
        return self._store.get(str(session_id or ""))

    def delete_session(self, session_id: str) -> int:
        delete = getattr(self._store, "delete_by_session", None)
        if not callable(delete):
            return 0
        return int(delete(str(session_id or "")))

    def members_for_session(self, session_id: str) -> list[str]:
        if self._rounds is None:
            return []
        getter = getattr(self._rounds, "get_active_by_session", None)
        round_ = getter(session_id) if callable(getter) else None
        if round_ is None:
            lister = getattr(self._rounds, "list_by_session", None)
            rows = lister(session_id, limit=1) if callable(lister) else []
            round_ = rows[0] if rows else None
        return list(getattr(round_, "member_role_ids", []) or [])

    def owner_for(self, member_role_ids: list[str]) -> str:
        members = [str(item) for item in member_role_ids if str(item)]
        if not members:
            return ""
        final = ""
        if self._org is not None and hasattr(self._org, "get_final_decider_role_id"):
            final = str(self._org.get_final_decider_role_id() or "")
        if final and final in members:
            return final
        if self._org is not None and hasattr(self._org, "get_top_level_role_ids"):
            for role_id in self._org.get_top_level_role_ids() or []:
                if str(role_id) in members:
                    return str(role_id)
        if self._org is not None and hasattr(self._org, "get_role"):
            for role_id in members:
                role = self._org.get_role(role_id)
                if bool(getattr(role, "is_final_decider", False)):
                    return role_id
        return members[0]

    def role_name(self, role_id: str) -> str:
        if self._org is not None and hasattr(self._org, "get_role"):
            role = self._org.get_role(str(role_id or ""))
            name = str(getattr(role, "name", "") or "").strip() if role is not None else ""
            if name:
                return name
        return str(role_id or "")

    def snapshot(self, goal: GroupGoal) -> dict[str, Any]:
        stage = goal.current_stage()
        return {
            "goal_id": goal.goal_id,
            "session_id": goal.session_id,
            "project_id": goal.project_id,
            "title": goal.title,
            "status": goal.status.value,
            "current": goal.current + 1 if goal.current >= 0 else 0,
            "total": len(goal.stages),
            "stage_title": stage.title if stage is not None else "",
            "owner_role_id": goal.owner_role_id,
            "owner_name": self.role_name(goal.owner_role_id),
            "revision": goal.revision,
            "updated_at": goal.updated_at,
        }

    def apply_action(
        self,
        session_id: str,
        role_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = dict(payload or {})
        members = self.members_for_session(session_id)
        goal = self.get(session_id)
        if action == "show":
            if goal is None:
                return {"ok": True, "message": "该群还没有目标。", "goal": None}
            return {
                "ok": True,
                "message": self.render_ledger(goal, role_id),
                "goal": self.snapshot(goal),
            }
        if action == "draft":
            goal = self.draft(
                session_id=session_id,
                project_id=str(data.get("project_id") or (goal.project_id if goal else "")),
                brief=str(data.get("brief") or ""),
                title=str(data.get("title") or ""),
                role_id=role_id,
                member_role_ids=members,
            )
        elif action == "start":
            goal = self.start(
                session_id=session_id,
                project_id=str(data.get("project_id") or ""),
                role_id=role_id,
                objective=str(data.get("objective") or ""),
                acceptance=str(data.get("acceptance") or ""),
                constraints=data.get("constraints"),
                stages=data.get("stages"),
                title=str(data.get("title") or ""),
                member_role_ids=members,
            )
        elif action == "revise":
            goal = self.revise(
                session_id=session_id,
                role_id=role_id,
                reason=str(data.get("reason") or ""),
                objective=data.get("objective"),
                acceptance=data.get("acceptance"),
                constraints=data.get("constraints"),
                stages=data.get("stages"),
                title=data.get("title"),
                member_role_ids=members,
            )
        elif action == "task":
            goal = self.task(
                session_id=session_id,
                role_id=role_id,
                target_role_id=str(data.get("bot") or ""),
                status=data.get("status"),
                note=data.get("note"),
                stage_number=data.get("stage"),
                member_role_ids=members,
            )
        elif action == "pass":
            goal = self.pass_stage(
                session_id=session_id,
                role_id=role_id,
                evidence=str(data.get("evidence") or ""),
            )
        elif action == "rework":
            goal = self.rework(
                session_id=session_id,
                role_id=role_id,
                reason=str(data.get("reason") or ""),
                target_role_id=str(data.get("bot") or ""),
                member_role_ids=members,
            )
        elif action == "complete":
            goal = self.complete(
                session_id=session_id,
                role_id=role_id,
                evidence=str(data.get("evidence") or ""),
                summary=str(data.get("summary") or ""),
            )
        elif action in ("pause", "resume", "stop"):
            goal = self.control(
                session_id=session_id,
                role_id=role_id,
                action=action,
                reason=str(data.get("reason") or ""),
            )
        else:
            raise GroupGoalError(f"未知 action：{action}")
        return {
            "ok": True,
            "message": self.render_ledger(goal, role_id),
            "goal": self.snapshot(goal),
        }

    def draft(
        self,
        *,
        session_id: str,
        project_id: str,
        brief: str,
        title: str,
        role_id: str,
        member_role_ids: list[str],
    ) -> GroupGoal:
        text = _require(brief, "brief")
        if len(text) > BRIEF_MAX:
            raise GroupGoalError(f"brief 超过 {BRIEF_MAX} 字")
        members = [str(item) for item in member_role_ids if str(item)]
        owner = self.owner_for(members)
        self._require_owner(role_id, owner)
        goal = self.get(session_id)
        if goal is not None and goal.status in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
            raise GroupGoalError("该群已有进行中的目标，先暂停或停止当前目标")
        if goal is not None and goal.status in (GoalStatus.COMPLETED, GoalStatus.STOPPED):
            goal = self._archive_and_new(goal, session_id, project_id, owner, members)
        if goal is None:
            goal = self._new_goal(session_id, project_id, owner, members)
        goal.brief = text
        goal.title = (_clip(title, TITLE_MAX, "title") or text.splitlines()[0])[:TITLE_MAX]
        goal.member_role_ids = members
        goal.owner_role_id = owner
        goal.status = GoalStatus.DRAFTING
        self._touch(goal, "draft", role_id, text)
        return self._save(goal)

    def start(
        self,
        *,
        session_id: str,
        role_id: str,
        objective: str,
        acceptance: str,
        constraints: Any,
        stages: Any,
        title: str,
        member_role_ids: list[str],
        project_id: str = "",
    ) -> GroupGoal:
        goal = self.get(session_id)
        members = [str(item) for item in member_role_ids if str(item)]
        if goal is None:
            owner = self.owner_for(members)
            self._require_owner(role_id, owner)
            goal = self._new_goal(session_id, project_id, owner, members)
        else:
            self._require_owner(role_id, goal.owner_role_id)
            if goal.status in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
                raise GroupGoalError("该群已有进行中的目标")
            if goal.status in (GoalStatus.COMPLETED, GoalStatus.STOPPED):
                goal = self._archive_and_new(
                    goal,
                    session_id,
                    goal.project_id,
                    goal.owner_role_id,
                    goal.member_role_ids,
                )
        members = members or goal.member_role_ids
        objective_text = _clip(_require(objective, "objective"), TEXT_MAX, "objective")
        acceptance_text = _clip(_require(acceptance, "acceptance"), TEXT_MAX, "acceptance")
        cleaned = self._clean_stages(stages, members)
        goal.objective = objective_text
        goal.acceptance = acceptance_text
        goal.constraints = _clean_constraints(constraints)
        goal.stages = cleaned
        goal.current = 0
        goal.status = GoalStatus.ACTIVE
        goal.title = _clip(title, TITLE_MAX, "title") or goal.title or objective_text[:TITLE_MAX]
        goal.member_role_ids = members
        goal.evidence = ""
        goal.summary = ""
        goal.reason = ""
        goal.stages[0].status = StageStatus.ACTIVE
        goal.brief = ""
        self._touch(goal, "start", role_id, goal.title)
        return self._save(goal)

    def revise(
        self,
        *,
        session_id: str,
        role_id: str,
        reason: str,
        objective: Any,
        acceptance: Any,
        constraints: Any,
        stages: Any,
        title: Any,
        member_role_ids: list[str],
    ) -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if goal.status not in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
            raise GroupGoalError("只有进行中或暂停的目标可以修订")
        reason_text = _clip(_require(reason, "reason"), REASON_MAX, "reason")
        members = [str(item) for item in member_role_ids if str(item)] or goal.member_role_ids
        next_title = goal.title
        if title is not None:
            next_title = _clip(title, TITLE_MAX, "title") or goal.title
        next_objective = goal.objective
        if objective is not None:
            next_objective = _clip(_require(objective, "objective"), TEXT_MAX, "objective")
        next_acceptance = goal.acceptance
        if acceptance is not None:
            next_acceptance = _clip(_require(acceptance, "acceptance"), TEXT_MAX, "acceptance")
        next_constraints = goal.constraints
        if constraints is not None:
            next_constraints = _clean_constraints(constraints)
        next_stages = goal.stages
        if stages is not None:
            cleaned = self._clean_stages(stages, members)
            keep = [stage for stage in goal.stages[: max(0, goal.current)]]
            for index, stage in enumerate(cleaned):
                stage.status = StageStatus.ACTIVE if index == 0 else StageStatus.PENDING
            next_stages = [*keep, *cleaned]
        goal.title = next_title
        goal.objective = next_objective
        goal.acceptance = next_acceptance
        goal.constraints = next_constraints
        goal.stages = next_stages
        goal.member_role_ids = members
        if stages is not None:
            goal.current = min(goal.current, max(0, len(goal.stages) - 1))
            for index, stage in enumerate(goal.stages):
                if index < goal.current:
                    stage.status = StageStatus.PASSED
                elif index == goal.current:
                    stage.status = StageStatus.ACTIVE
                else:
                    stage.status = StageStatus.PENDING
        self._touch(goal, "revise", role_id, reason_text)
        return self._save(goal)

    def task(
        self,
        *,
        session_id: str,
        role_id: str,
        target_role_id: str,
        status: Any,
        note: Any,
        stage_number: Any,
        member_role_ids: list[str],
    ) -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if goal.status != GoalStatus.ACTIVE:
            raise GroupGoalError("只有进行中的目标可以更新任务")
        stage = self._stage_for(goal, stage_number)
        target = self._resolve_member(target_role_id, member_role_ids or goal.member_role_ids)
        tasks = [item for item in stage.tasks if item.role_id == target]
        if not tasks:
            raise GroupGoalError(f"{self.role_name(target)} 在当前阶段没有任务")
        status_text = str(status or "").strip()
        note_text = str(note or "").strip()
        if not status_text and not note_text:
            raise GroupGoalError("status 或 note 至少提供一个")
        if status_text:
            try:
                next_status = TaskStatus(status_text)
            except ValueError as exc:
                raise GroupGoalError(f"未知任务状态：{status_text}") from exc
            for item in tasks:
                item.status = next_status
        if note_text:
            if len(note_text) > NOTE_MAX:
                raise GroupGoalError(f"note 超过 {NOTE_MAX} 字")
            for item in tasks:
                item.note = note_text
        self._touch(goal, "task", role_id, note_text or status_text)
        return self._save(goal)

    def pass_stage(self, *, session_id: str, role_id: str, evidence: str) -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if goal.status != GoalStatus.ACTIVE:
            raise GroupGoalError("只有进行中的目标可以验收阶段")
        stage = goal.current_stage()
        if stage is None:
            raise GroupGoalError("所有阶段已通过，调用 complete 结束目标")
        evidence_text = _clip(_require(evidence, "evidence"), TEXT_MAX, "evidence")
        stage.status = StageStatus.PASSED
        stage.evidence = evidence_text
        for item in stage.tasks:
            item.status = TaskStatus.DONE
        goal.current += 1
        next_stage = goal.current_stage()
        if next_stage is not None:
            next_stage.status = StageStatus.ACTIVE
        self._touch(goal, "pass", role_id, evidence_text)
        return self._save(goal)

    def rework(
        self,
        *,
        session_id: str,
        role_id: str,
        reason: str,
        target_role_id: str,
        member_role_ids: list[str],
    ) -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if goal.status != GoalStatus.ACTIVE:
            raise GroupGoalError("只有进行中的目标可以返工")
        stage = goal.current_stage()
        if stage is None:
            raise GroupGoalError("所有阶段已通过，不能返工")
        reason_text = _clip(_require(reason, "reason"), REASON_MAX, "reason")
        target = ""
        if str(target_role_id or "").strip():
            target = self._resolve_member(target_role_id, member_role_ids or goal.member_role_ids)
        for item in stage.tasks:
            if target and item.role_id != target:
                continue
            item.status = TaskStatus.PENDING
            item.note = ""
        stage.reworks += 1
        stage.feedback = reason_text
        self._touch(goal, "rework", role_id, reason_text)
        return self._save(goal)

    def complete(self, *, session_id: str, role_id: str, evidence: str, summary: str) -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if goal.status != GoalStatus.ACTIVE:
            raise GroupGoalError("只有进行中的目标可以完成")
        if any(stage.status != StageStatus.PASSED for stage in goal.stages):
            raise GroupGoalError("还有阶段未通过，不能完成目标")
        evidence_text = _clip(_require(evidence, "evidence"), TEXT_MAX, "evidence")
        summary_text = _clip(_require(summary, "summary"), TEXT_MAX, "summary")
        goal.status = GoalStatus.COMPLETED
        goal.evidence = evidence_text
        goal.summary = summary_text
        self._touch(goal, "complete", role_id, summary_text)
        return self._save(goal)

    def control(self, *, session_id: str, role_id: str, action: str, reason: str = "") -> GroupGoal:
        goal = self._require_goal(session_id)
        self._require_owner(role_id, goal.owner_role_id)
        if action == "pause":
            if goal.status != GoalStatus.ACTIVE:
                raise GroupGoalError("只有进行中的目标可以暂停")
            goal.status = GoalStatus.PAUSED
        elif action == "resume":
            if goal.status != GoalStatus.PAUSED:
                raise GroupGoalError("只有暂停的目标可以恢复")
            goal.status = GoalStatus.ACTIVE
        elif action == "stop":
            if goal.status not in (GoalStatus.DRAFTING, GoalStatus.ACTIVE, GoalStatus.PAUSED):
                raise GroupGoalError("当前目标不能停止")
            goal.status = GoalStatus.STOPPED
            goal.reason = _clip(_require(reason, "reason"), REASON_MAX, "reason")
        else:
            raise GroupGoalError(f"未知控制动作：{action}")
        self._touch(goal, action, role_id, reason)
        return self._save(goal)

    def mark_working(self, session_id: str, role_id: str) -> None:
        self._mark_task(session_id, role_id, TaskStatus.PENDING, TaskStatus.WORKING)

    def mark_reported(self, session_id: str, role_id: str) -> None:
        self._mark_task(session_id, role_id, TaskStatus.WORKING, TaskStatus.REPORTED)

    def mark_reset(self, session_id: str, role_id: str, note: str = "") -> None:
        self._mark_task(session_id, role_id, TaskStatus.WORKING, TaskStatus.PENDING, note=note)

    def _mark_task(
        self,
        session_id: str,
        role_id: str,
        from_status: TaskStatus,
        to_status: TaskStatus,
        *,
        note: str = "",
    ) -> None:
        goal = self.get(session_id)
        if goal is None or goal.status != GoalStatus.ACTIVE:
            return
        stage = goal.current_stage()
        if stage is None:
            return
        changed = False
        for item in stage.tasks:
            if item.role_id != role_id or item.status != from_status:
                continue
            item.status = to_status
            item.note = note[:NOTE_MAX]
            changed = True
        if changed:
            goal.revision += 1
            goal.updated_at = time.time()
            self._save(goal, event=None)

    def render_block(self, session_id: str, viewer_role_id: str) -> str:
        goal = self.get(session_id)
        if goal is None or goal.status in (GoalStatus.COMPLETED, GoalStatus.STOPPED):
            return ""
        is_owner = viewer_role_id == goal.owner_role_id
        if goal.status == GoalStatus.DRAFTING:
            if is_owner:
                lines = [
                    '<group_goal status="drafting">',
                    f"用户希望这个群达成目标：{goal.brief}",
                    "先对齐目标、完成标准、约束和成员分工，再调用 group_goal start。",
                    "只问用户无法自行查明的决策；能查的事实自己查。",
                    "</group_goal>",
                ]
            else:
                lines = [
                    '<group_goal status="drafting">',
                    f"群主正在和用户对齐目标：{goal.brief[:300]}",
                    "被问到时提供事实，不要自行改写目标。",
                    "</group_goal>",
                ]
            return "\n".join(lines)
        if is_owner:
            lines = ['<group_goal status="' + goal.status.value + '">', *self._ledger_lines(goal)]
            lines.extend([self._duty(goal), "</group_goal>"])
            return "\n".join(lines)
        stage = goal.current_stage()
        lines = [
            f'<group_goal status="{goal.status.value}" stage="{goal.current + 1}/{len(goal.stages)}">',
            f"群目标：{goal.title} · {goal.objective[:600]}",
        ]
        if goal.constraints:
            lines.append("规则：" + "；".join(goal.constraints))
        if stage is None:
            lines.append("所有阶段已通过，群主正在做最终验收。")
        else:
            lines.append(
                f"当前阶段 {goal.current + 1}「{stage.title}」通过标准：{stage.acceptance}"
            )
            own = [item for item in stage.tasks if item.role_id == viewer_role_id]
            if own:
                for item in own:
                    lines.append(
                        f"你的任务：{item.task}（{TASK_LABEL.get(item.status, item.status.value)}）"
                    )
            else:
                lines.append("你在当前阶段没有任务。")
        lines.append("只围绕这个目标工作；完成时在回复里给出可核对的证据。")
        if goal.status == GoalStatus.PAUSED:
            lines.append("目标已暂停：完成手上的事，不要开始新的阶段工作。")
        lines.append("</group_goal>")
        return "\n".join(lines)

    def render_ledger(self, goal: GroupGoal, viewer_role_id: str) -> str:
        if viewer_role_id == goal.owner_role_id:
            return "\n".join(self._ledger_lines(goal))
        stage = goal.current_stage()
        if stage is None:
            return f"目标「{goal.title}」所有阶段已通过，等待群主最终验收。"
        own = [item for item in stage.tasks if item.role_id == viewer_role_id]
        tasks = "；".join(
            f"{item.task}（{TASK_LABEL.get(item.status, item.status.value)}）" for item in own
        )
        return (
            f"目标「{goal.title}」当前阶段 {goal.current + 1}/{len(goal.stages)}："
            f"{stage.title}；通过标准：{stage.acceptance}；你的任务：{tasks or '无'}。"
        )

    def _ledger_lines(self, goal: GroupGoal) -> list[str]:
        lines = [
            f"目标：{goal.title}",
            f"Objective：{goal.objective}",
            f"Done when：{goal.acceptance}",
        ]
        if goal.constraints:
            lines.append("规则：")
            lines.extend(f"- {rule}" for rule in goal.constraints)
        lines.append("阶段：")
        for index, stage in enumerate(goal.stages):
            number = index + 1
            if stage.status == StageStatus.PASSED:
                lines.append(f"[已通过] {number}. {stage.title}")
                continue
            if index == goal.current:
                lines.append(f"[当前] {number}. {stage.title} · 通过标准：{stage.acceptance}")
                for item in stage.tasks:
                    note = f" · {item.note}" if item.note else ""
                    lines.append(
                        f"  - {self.role_name(item.role_id)}：{item.task}"
                        f" [{TASK_LABEL.get(item.status, item.status.value)}]{note}"
                    )
                if stage.reworks:
                    lines.append(f"  已返工 {stage.reworks} 次，最近反馈：{stage.feedback}")
                continue
            names = sorted({self.role_name(item.role_id) for item in stage.tasks})
            lines.append(f"[待开始] {number}. {stage.title}：{', '.join(names)}")
        revisions = [
            str(event.get("text") or "")
            for event in goal.events
            if event.get("kind") == "revise" and event.get("text")
        ][-3:]
        if revisions:
            lines.append("最近修订：" + " / ".join(revisions))
        return lines

    @staticmethod
    def _duty(goal: GroupGoal) -> str:
        if goal.status == GoalStatus.PAUSED:
            return "目标已暂停：不要派发阶段任务，和用户对话，用 revise 写入修正，等待用户恢复。"
        if goal.current_stage() is None:
            return "所有阶段已通过：核对总体验收标准，调用 group_goal complete。"
        return (
            "你负责推进目标：把当前阶段任务用 @成员 派下去；成员回报后按通过标准核对证据，"
            "调用 group_goal pass 或 rework；用户纠正方向时先 revise。"
        )

    def _clean_stages(self, values: Any, member_role_ids: list[str]) -> list[GoalStage]:
        if not isinstance(values, list) or not values:
            raise GroupGoalError("stages 不能为空")
        if len(values) > STAGES_MAX:
            raise GroupGoalError(f"stages 最多 {STAGES_MAX} 个")
        stages: list[GoalStage] = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise GroupGoalError(f"stage {index + 1} 必须是对象")
            title = _clip(
                _require(item.get("title"), f"stage {index + 1} title"), STAGE_TITLE_MAX, "title"
            )
            acceptance = _clip(
                _require(item.get("acceptance"), f"stage {index + 1} acceptance"),
                STAGE_ACCEPTANCE_MAX,
                "acceptance",
            )
            tasks = item.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                raise GroupGoalError(f"stage {index + 1} tasks 不能为空")
            if len(tasks) > TASKS_MAX:
                raise GroupGoalError(f"stage {index + 1} tasks 最多 {TASKS_MAX} 个")
            stage_tasks: list[GoalTask] = []
            for entry in tasks:
                if not isinstance(entry, dict):
                    raise GroupGoalError(f"stage {index + 1} 的 task 必须是对象")
                task_text = _clip(_require(entry.get("task"), "task"), TASK_MAX, "task")
                role_id = self._resolve_member(str(entry.get("bot") or ""), member_role_ids)
                stage_tasks.append(GoalTask(uuid.uuid4().hex, role_id, task_text))
            stages.append(GoalStage(uuid.uuid4().hex, title, acceptance, stage_tasks))
        return stages

    def _resolve_member(self, value: str, member_role_ids: list[str]) -> str:
        text = str(value or "").strip()
        if not text:
            raise GroupGoalError("成员不能为空")
        members = [str(item) for item in member_role_ids if str(item)]
        for role_id in members:
            if role_id == text or self.role_name(role_id).lower() == text.lower():
                return role_id
        raise GroupGoalError(f"{text} 不是当前群成员")

    def _stage_for(self, goal: GroupGoal, stage_number: Any) -> GoalStage:
        if stage_number is None:
            stage = goal.current_stage()
            if stage is None:
                raise GroupGoalError("当前没有进行中的阶段")
            return stage
        try:
            index = int(stage_number) - 1
        except (TypeError, ValueError) as exc:
            raise GroupGoalError("stage 必须是数字") from exc
        if index < 0 or index >= len(goal.stages):
            raise GroupGoalError(f"没有第 {stage_number} 阶段")
        return goal.stages[index]

    def _require_goal(self, session_id: str) -> GroupGoal:
        goal = self.get(session_id)
        if goal is None:
            raise GroupGoalError("该群还没有目标")
        return goal

    @staticmethod
    def _require_owner(role_id: str, owner_role_id: str) -> None:
        if not owner_role_id or str(role_id or "") != owner_role_id:
            raise GroupGoalError("只有群主或团队顶层角色可以修改目标")

    def _new_goal(
        self, session_id: str, project_id: str, owner: str, members: list[str]
    ) -> GroupGoal:
        return GroupGoal(
            goal_id=f"goal-{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            project_id=project_id,
            owner_role_id=owner,
            member_role_ids=members,
        )

    def _archive_and_new(
        self, goal: GroupGoal, session_id: str, project_id: str, owner: str, members: list[str]
    ) -> GroupGoal:
        archived = dict(goal.to_dict())
        next_goal = self._new_goal(session_id, project_id, owner, members)
        next_goal.past_goals = [*goal.past_goals, archived][-PAST_MAX:]
        return next_goal

    def _touch(self, goal: GroupGoal, kind: str, role_id: str, text: str = "") -> None:
        goal.revision += 1
        goal.updated_at = time.time()
        goal.events = [
            *goal.events,
            {"kind": kind, "by": role_id, "text": text[:300], "at": goal.updated_at},
        ][-EVENTS_MAX:]

    def _save(self, goal: GroupGoal, *, event: str | None = "goal") -> GroupGoal:
        saved = self._store.save(goal)
        if self._broadcast is not None and event is not None:
            try:
                self._broadcast(self.snapshot(saved))
            except Exception:  # noqa: BLE001 — 广播失败不影响目标状态
                pass
        return saved
