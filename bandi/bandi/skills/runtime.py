# 功能：SOP 技能卡任务帧栈引擎（建帧/推进/挂起/恢复/完成 + 槽位水合 + 待收集字段裁剪）。
# core/task_frame_policy.py（帧查询与选帧）+ core/slot_hydration_policy.py（记忆补水合）。
# 「调用方显式调用」——引擎只做纯规则推进（图出边 + priority 决胜），**不猜条件**：
# 出边全部带条件或多候选同优先级时安全收束报错，由调用方显式指定后继。
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bandi.core.exceptions import BandiError
from bandi.skills.card import SkillCard, SkillGraphNode
from bandi.skills.serializer import SkillCardLoadError, load_card

HYDRATABLE_PROFILE_FIELD = "user_name"

MAX_COMPLETED_FRAMES = 100

_PROFILE_MEMORY_KIND = "profile"
_PROFILE_NAME_KEY = "preferred_name"
_PROFILE_NAME_MAX_LENGTH = 40


class SkillFrameError(BandiError):
    """帧栈操作失败（基类）。"""


class SkillFrameNotFoundError(SkillFrameError):
    """目标帧或技能卡不存在。"""


class SkillFrameTransitionError(SkillFrameError):
    """帧状态迁移非法（无激活帧/出边歧义/指定后继无对应边）。"""


class SkillFrameStatus(StrEnum):
    ACTIVE = "active"  # 功能：当前正在执行的帧
    SUSPENDED = "suspended"  # 功能：被切换下去、保留现场等待恢复的帧
    COMPLETED = "completed"  # 功能：已到达流程出口的帧


class SkillFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str  # 功能：任务帧标识（同 scope 内唯一，重复入队幂等）
    skill_id: str  # 功能：所属 SOP 技能卡标识
    node_id: str | None = None  # 功能：当前停留的图节点标识
    status: SkillFrameStatus  # 功能：帧生命周期状态（构造时必须显式给定）
    slots: dict[str, Any] = Field(default_factory=dict)  # 功能：本帧已收集槽位
    awaiting_fields: list[str] = Field(default_factory=list)  # 功能：本帧尚需收集的信息项
    summary: str = ""  # 功能：帧摘要（跨轮上下文压缩用）
    last_agent_question: str = ""  # 功能：上一次向用户提出的问题
    resume_policy: str | None = None  # 功能：恢复策略标记
    created_at: str  # 功能：创建时刻（ISO 8601）
    updated_at: str  # 功能：最近更新时刻（ISO 8601）


class SkillFrameStack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str  # 功能：帧栈作用域（调用方语义，如会话/员工标识）
    active: SkillFrame | None = None  # 功能：当前激活帧（同时最多一个）
    pending: list[SkillFrame] = Field(default_factory=list)  # 功能：挂起/排队帧（后进先出恢复）
    completed: list[SkillFrame] = Field(default_factory=list)  # 功能：已完成帧历史（审计用）
    revision: int = 0  # 功能：落盘版本号（并发写乐观锁；load 读回、save 比对并递增）


class SkillRuntime:
    """功能：任务帧栈引擎——一次只激活一个帧，其余帧挂起在栈上，支持任意切换与恢复。"""

    def __init__(self, *, skills_dir: str = "~/.bandi/skills") -> None:
        self.skills_dir = skills_dir

    def load_card(self, skill_id: str) -> SkillCard:
        """功能：从 skills_dir 读回技能卡（缺失/非法即显式失败）。"""
        path = Path(self.skills_dir).expanduser() / f"{skill_id}.card.json"
        try:
            return load_card(path)
        except SkillCardLoadError as exc:
            raise SkillFrameNotFoundError(f"未找到可执行的技能卡: {skill_id}: {exc}") from exc

    def new_stack(self, scope_id: str) -> SkillFrameStack:
        """功能：创建空帧栈（scope_id 由调用方定义语义，引擎不解释）。"""
        return SkillFrameStack(scope_id=scope_id)

    def start(
        self,
        stack: SkillFrameStack,
        card: SkillCard,
        *,
        task_id: str | None = None,
        slots: dict[str, Any] | None = None,
        resume_policy: str | None = None,
    ) -> SkillFrameStack:
        """功能：从卡片起点新建激活帧（同 task_id 重复调用幂等；原激活帧自动入栈挂起）。"""
        resolved_id = task_id or self._new_task_id()
        if self._find_frame(stack, resolved_id) is not None:
            return stack
        resolved_slots = dict(slots or {})
        now = _now()
        frame = SkillFrame(
            task_id=resolved_id,
            skill_id=card.skill_id,
            node_id=card.start_node_id,
            status=SkillFrameStatus.ACTIVE,
            slots=resolved_slots,
            awaiting_fields=self.awaiting_fields(card, resolved_slots, card.start_node_id),
            resume_policy=resume_policy,
            created_at=now,
            updated_at=now,
        )
        return SkillFrameStack(
            scope_id=stack.scope_id,
            active=frame,
            pending=self._with_suspended_active(stack),
            completed=list(stack.completed),
            revision=stack.revision,
        )

    def advance(
        self,
        stack: SkillFrameStack,
        card: SkillCard,
        *,
        next_node_id: str | None = None,
    ) -> SkillFrameStack:
        """功能：推进激活帧一拍——显式后继优先，否则按无条件出边 priority 决胜；
        到达 terminal 节点即闭环入 completed。歧义/非法后继一律报错且不改栈。"""
        active = stack.active
        if active is None:
            raise SkillFrameTransitionError("当前没有激活的任务帧，无法推进。")
        if card.skill_id != active.skill_id:
            raise SkillFrameTransitionError(
                f"卡片 skill_id={card.skill_id} 与激活帧 skill_id={active.skill_id} 不一致，"
                "拒绝按错误流程图推进。"
            )
        current = active.node_id
        target = next_node_id or self._resolve_next_node(card, current)
        if not self._has_edge(card, current, target):
            raise SkillFrameTransitionError(f"节点 {current} 没有指向 {target} 的出边。")
        slots = dict(active.slots)
        moved = active.model_copy(
            update={
                "node_id": target,
                "awaiting_fields": self.awaiting_fields(card, slots, target),
                "updated_at": _now(),
            }
        )
        if target in set(card.terminal_node_ids):
            done = moved.model_copy(update={"status": SkillFrameStatus.COMPLETED})
            return SkillFrameStack(
                scope_id=stack.scope_id,
                active=None,
                pending=list(stack.pending),
                completed=_trim_completed([*stack.completed, done]),
                revision=stack.revision,
            )
        return SkillFrameStack(
            scope_id=stack.scope_id,
            active=moved,
            pending=list(stack.pending),
            completed=list(stack.completed),
            revision=stack.revision,
        )

    def suspend(self, stack: SkillFrameStack) -> SkillFrameStack:
        """功能：挂起激活帧（现场整体保留，槽位与待收集字段不变）。"""
        if stack.active is None:
            raise SkillFrameTransitionError("当前没有激活的任务帧，无法挂起。")
        frame = _with_status(stack.active, SkillFrameStatus.SUSPENDED)
        return SkillFrameStack(
            scope_id=stack.scope_id,
            active=None,
            pending=[*stack.pending, frame],
            completed=list(stack.completed),
            revision=stack.revision,
        )

    def resume(self, stack: SkillFrameStack, task_id: str) -> SkillFrameStack:
        """功能：从挂起栈恢复指定帧为激活态；原激活帧自动入栈挂起（栈式切换）。"""
        target: SkillFrame | None = None
        remaining: list[SkillFrame] = []
        for frame in stack.pending:
            if target is None and frame.task_id == task_id:
                target = frame
                continue
            remaining.append(frame)
        if target is None:
            raise SkillFrameNotFoundError(f"挂起栈中没有 task_id={task_id} 的任务帧。")
        if stack.active is not None:
            remaining.append(_with_status(stack.active, SkillFrameStatus.SUSPENDED))
        restored = _with_status(target, SkillFrameStatus.ACTIVE)
        return SkillFrameStack(
            scope_id=stack.scope_id,
            active=restored,
            pending=remaining,
            completed=list(stack.completed),
            revision=stack.revision,
        )

    def complete(self, stack: SkillFrameStack) -> SkillFrameStack:
        """功能：完成激活帧（入 completed 历史，激活位清空）。"""
        if stack.active is None:
            raise SkillFrameTransitionError("当前没有激活的任务帧，无法完成。")
        done = _with_status(stack.active, SkillFrameStatus.COMPLETED)
        return SkillFrameStack(
            scope_id=stack.scope_id,
            active=None,
            pending=list(stack.pending),
            completed=_trim_completed([*stack.completed, done]),
            revision=stack.revision,
        )

    def await_field_names(self, card: SkillCard) -> list[str]:
        """功能：卡片声明的全部信息项（required_info ∪ 各节点 expected_user_info）。"""
        fields = list(card.required_info)
        for node in card.nodes:
            fields.extend(node.expected_user_info)
        return _dedupe(fields)

    def awaiting_fields(
        self,
        card: SkillCard,
        slots: dict[str, Any],
        node_id: str | None = None,
    ) -> list[str]:
        """功能：当前步骤尚需收集的信息项（required_info ∪ 当前节点 expected_user_info
        去掉已有值的项；node_id 为空时只看 required_info）。"""
        fields = list(card.required_info)
        if node_id:
            node = _find_node(card, node_id)
            if node is not None:
                fields.extend(node.expected_user_info)
        return [field for field in _dedupe(fields) if not _has_value(slots, field)]

    def hydrate_slots(
        self,
        card: SkillCard,
        slots: dict[str, Any],
        memory_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """功能：按记忆补全缺失槽位（仅补卡片声明且当前无值的项；**不覆盖已有值**）。"""
        if HYDRATABLE_PROFILE_FIELD not in self.await_field_names(card):
            return {}
        if _has_value(slots, HYDRATABLE_PROFILE_FIELD):
            return {}
        name = _profile_name(memory_context)
        if not name:
            return {}
        return {HYDRATABLE_PROFILE_FIELD: name}

    def _resolve_next_node(self, card: SkillCard, current: str | None) -> str:
        """功能：纯规则选后继——无条件出边中 priority 最大者；全条件/同优先级多目标即报错。"""
        outgoing = [edge for edge in card.edges if edge.source_node_id == current]
        if not outgoing:
            raise SkillFrameTransitionError(f"节点 {current} 没有出边，无法推进（已是流程出口）。")
        unconditional = [edge for edge in outgoing if not (edge.condition or "").strip()]
        if not unconditional:
            raise SkillFrameTransitionError(
                f"节点 {current} 的出边全部带条件，引擎不猜测条件：请用 next_node_id 显式指定后继。"
            )
        best = max(edge.priority for edge in unconditional)
        targets = {edge.next_node_id for edge in unconditional if edge.priority == best}
        if len(targets) > 1:
            raise SkillFrameTransitionError(
                f"节点 {current} 在 priority={best} 上有多个候选后继，需显式指定后继。"
            )
        return targets.pop()

    @staticmethod
    def _has_edge(card: SkillCard, source: str | None, target: str) -> bool:
        return any(
            edge.source_node_id == source and edge.next_node_id == target for edge in card.edges
        )

    def _with_suspended_active(self, stack: SkillFrameStack) -> list[SkillFrame]:
        """功能：把当前激活帧转为挂起并追加到 pending（保留原顺序）。"""
        frames = list(stack.pending)
        if stack.active is not None:
            frames.append(_with_status(stack.active, SkillFrameStatus.SUSPENDED))
        return frames

    @staticmethod
    def _find_frame(stack: SkillFrameStack, task_id: str | None) -> SkillFrame | None:
        """功能：在激活/挂起/已完成三个区段中查找 task_id 对应的帧。"""
        if not task_id:
            return None
        buckets: list[SkillFrame] = list(stack.pending)
        if stack.active is not None:
            buckets.append(stack.active)
        buckets.extend(stack.completed)
        for frame in buckets:
            if frame.task_id == task_id:
                return frame
        return None

    @staticmethod
    def _new_task_id() -> str:
        return f"task_{uuid.uuid4().hex[:12]}"


def render_active_frame(stack: SkillFrameStack) -> str | None:
    """功能：——把激活帧渲染为单段 SOP 状态块（agent 主循环注入用；
    无激活帧返回 None）。纯读渲染，不改帧栈。"""
    frame = stack.active
    if frame is None:
        return None
    segments = [f"技能 {frame.skill_id} 进行中：当前节点 {frame.node_id or '(未定)'}"]
    if frame.awaiting_fields:
        segments.append(f"待收集：{'、'.join(frame.awaiting_fields)}")
    if frame.last_agent_question:
        segments.append(f"最近提问：{frame.last_agent_question}")
    if frame.summary:
        segments.append(f"帧摘要：{frame.summary}")
    return "[SOP 帧状态] " + "；".join(segments)


def _with_status(frame: SkillFrame, status: SkillFrameStatus) -> SkillFrame:
    """功能：复制帧并改写状态与更新时间（其余字段逐字保留）。"""
    return frame.model_copy(update={"status": status, "updated_at": _now()})


def _trim_completed(frames: list[SkillFrame]) -> list[SkillFrame]:
    """功能：completed 历史滑动窗口——保留最近 MAX_COMPLETED_FRAMES 条（列表尾部最新）。"""
    if len(frames) <= MAX_COMPLETED_FRAMES:
        return frames
    return frames[-MAX_COMPLETED_FRAMES:]


def _find_node(card: SkillCard, node_id: str) -> SkillGraphNode | None:
    for node in card.nodes:
        if node.node_id == node_id:
            return node
    return None


def _dedupe(values: list[str]) -> list[str]:
    """功能：按首次出现顺序去重（保持卡片声明顺序，稳定可断言）。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _has_value(slots: dict[str, Any], field: str) -> bool:
    value = slots.get(field)
    return value is not None and value != "" and value != []


def _profile_name(memory_context: list[dict[str, Any]]) -> str:
    """功能：从记忆上下文取 profile 型 preferred_name（超长截断，空即无候选）。"""
    for memory in memory_context:
        if memory.get("kind") != _PROFILE_MEMORY_KIND:
            continue
        metadata = memory.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        content = str(memory.get("content") or "").strip()
        if key == _PROFILE_NAME_KEY and content:
            return content[:_PROFILE_NAME_MAX_LENGTH]
    return ""


def _now() -> str:
    return datetime.now().isoformat()
