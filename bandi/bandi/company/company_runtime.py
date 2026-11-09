from __future__ import annotations

import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from bandi.company.phase import Phase, is_terminal, kanban_column
from bandi.company.runtime_identity import CompanyRuntimeIdentity
from bandi.company.session_scoping import scoped_member_session_id, scoped_queue_key
from bandi.company.work_item import WorkItem, WorkItemTurnType

_M = TypeVar("_M")

_ROLE_RUNTIME_PREFIX = "role-runtime::"

_WORK_ITEM_TAG_PREFIX = "work-item::"
_REVIEW_WORK_ITEM_TAG_PREFIX = "review-work-item::"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
_STATUS_ACTIVATING = frozenset({"reserved", "booting", "draining"})
_STATUS_DEAD = frozenset({"dead"})
_STATUS_RANK = {STATUS_IDLE: 0, STATUS_RUNNING: 1, STATUS_BLOCKED: 2}
_UNKNOWN_STATUS_RANK = 9


def normalize_role_runtime_status(
    status: str | None,
    focused_work_item_id: str | None = "",
    *,
    default: str = STATUS_IDLE,
) -> str:
    text = str(status or "").strip().lower()
    focused = bool(str(focused_work_item_id or "").strip())
    if text == STATUS_IDLE:
        return STATUS_IDLE
    if text == STATUS_RUNNING:
        return STATUS_RUNNING if focused else STATUS_IDLE
    if text == STATUS_BLOCKED:
        return STATUS_BLOCKED
    if text == "":
        return STATUS_BLOCKED if focused else default
    if text == "cold":
        return STATUS_IDLE
    if text in _STATUS_ACTIVATING:
        return STATUS_RUNNING if focused else STATUS_IDLE
    if text in _STATUS_DEAD:
        return STATUS_BLOCKED
    return default


@dataclass
class CompanyRuntimeState:
    """单次公司 run 的可变调度状态容器。"""

    member_sessions: dict[str, CompanyMemberSession] = field(default_factory=dict)
    role_queues: dict[str, deque[str]] = field(default_factory=lambda: defaultdict(deque))
    queued_work_item_ids: set[str] = field(default_factory=set)
    claimed_work_item_ids: set[str] = field(default_factory=set)
    home_team_instance_by_role: dict[str, str] = field(default_factory=dict)


def canonical_role_session_id(
    *,
    run_id: str,
    role_id: str,
    team_instance_id: str = "",
) -> str:
    """生成规范角色会话 id：``role-runtime::{run_id}::{team}::{role_id}``
    （多团队隔离：同 run 同 role 不同 team 会话互异；team 为空保持旧两段格式）。"""
    rid = str(run_id or "").strip()
    if not rid:
        raise ValueError("canonical_role_session_id: run_id is required")
    role = str(role_id or "").strip()
    if not role:
        raise ValueError("canonical_role_session_id: role_id is required")
    team = str(team_instance_id or "").strip()
    if team:
        return f"{_ROLE_RUNTIME_PREFIX}{rid}::{team}::{role}"
    return f"{_ROLE_RUNTIME_PREFIX}{rid}::{role}"


@dataclass
class CompanyMemberSession:
    """成员会话：角色在某次公司 run 中长期持有的执行会话状态。"""

    member_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role_session_id: str = ""
    role_id: str = ""
    employee_id: str = ""
    status: str = "idle"
    current_task_id: str = ""
    focused_work_item_id: str = ""
    background_work_item_ids: list[str] = field(default_factory=list)
    manager_role_ids: list[str] = field(default_factory=list)
    current_work_item: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CompanyRuntime:
    project_id: str
    session_scope_id: str
    role_id: str
    employee_id: str
    identities: dict[str, CompanyRuntimeIdentity] = field(default_factory=dict)
    work_item_queue: deque[WorkItem] = field(default_factory=deque)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    team_instance_id: str = ""
    # 员工持久历史键（非空 = 会话跨任务共享；只影响会话拼接，队列
    # frontier tag 仍按 session_scope_id——`_pop_tag`/`scoped_queue_key` 不消费本字段）
    history_scope_id: str = ""
    # 功能：DAG 依赖门——按 work item id 判断依赖是否已放行（缺省不启用）
    dependency_ready: Callable[[str], bool] | None = field(default=None, repr=False)
    _metadata: dict[str, Any] = field(default_factory=dict)
    _default_state: CompanyRuntimeState = field(default_factory=CompanyRuntimeState, repr=False)
    _state_var: ContextVar[CompanyRuntimeState | None] = field(
        default_factory=lambda: ContextVar("company-runtime-state", default=None),
        repr=False,
    )

    @property
    def state(self) -> CompanyRuntimeState:
        current = self._state_var.get()
        return self._default_state if current is None else current

    def create_state(self) -> CompanyRuntimeState:
        return CompanyRuntimeState()

    def use_state(self, state: CompanyRuntimeState) -> Token[CompanyRuntimeState | None]:
        return self._state_var.set(state)

    def reset_state(self, token: Token[CompanyRuntimeState | None]) -> None:
        self._state_var.reset(token)

    def member_session_id(self) -> str:
        return scoped_member_session_id(
            project_id=self.project_id,
            session_scope_id=self.session_scope_id,
            role_id=self.role_id,
            employee_id=self.employee_id,
            team_instance_id=self.team_instance_id,
            history_scope_id=self.history_scope_id,
        )

    def set_home_team(self, role_id: str, team_id: str) -> None:
        """登记角色归属团队（home team）——调度队列/会话按此隔离。"""
        role = (role_id or "").strip()
        team = (team_id or "").strip()
        if not role:
            return
        if team:
            self.state.home_team_instance_by_role[role] = team

    def sync_home_teams_from_roles(self, roles: list[Any]) -> None:
        """从组织角色列表批量登记 home team（来源：OrgEngine.list_roles()）。"""
        for role in roles:
            team = (getattr(role, "team_id", "") or "").strip()
            rid = (getattr(role, "id", "") or "").strip()
            if rid and team:
                self.state.home_team_instance_by_role[rid] = team

    def register_identity(self, identity: CompanyRuntimeIdentity) -> None:
        self.identities[identity.member_session_id()] = identity

    def resolve_identity(self, session_id: str) -> CompanyRuntimeIdentity | None:
        return self.identities.get(session_id)

    def push_to_queue(self, item: WorkItem) -> None:
        self.work_item_queue.append(item)

    def pop_from_queue(self) -> WorkItem | None:
        if not self.work_item_queue:
            return None
        return self.work_item_queue.popleft()

    def list_queue(self) -> list[WorkItem]:
        return list(self.work_item_queue)

    def queue_size(self) -> int:
        return len(self.work_item_queue)

    def metadata(self, key: str, default: _M | None = None) -> _M | None:
        return self._metadata.get(key, default)

    def metadata_snapshot(self) -> dict[str, Any]:
        return dict(self._metadata)

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def count_work_items_by_phase(self) -> dict[str, int]:
        return Counter(item.phase.value for item in self.work_item_queue)

    def find_work_item(self, item_id: str) -> WorkItem | None:
        for item in self.work_item_queue:
            if item.id == item_id:
                return item
        return None

    def _new_member_session(self, sid: str, role_id: str, employee_id: str) -> CompanyMemberSession:
        session = CompanyMemberSession(
            member_session_id=sid,
            role_session_id=canonical_role_session_id(
                run_id=self.project_id, role_id=role_id, team_instance_id=self.team_instance_id
            ),
            role_id=role_id,
            employee_id=employee_id,
        )
        self.state.member_sessions[sid] = session
        return session

    def ensure_member_session(self) -> CompanyMemberSession:
        sid = self.member_session_id()
        session = self.state.member_sessions.get(sid)
        if session is None:
            session = self._new_member_session(sid, self.role_id, self.employee_id)
        return session

    def session_for_work_item(self, item: WorkItem) -> CompanyMemberSession:
        session = self.ensure_member_session()
        session.current_task_id = item.id
        session.focused_work_item_id = item.id
        session.updated_at = datetime.now(UTC)
        return session

    @staticmethod
    def _set_member_session_status(
        session: CompanyMemberSession,
        status: str,
        *,
        focused_work_item_id: str | None = None,
    ) -> str:
        if focused_work_item_id is not None:
            session.focused_work_item_id = str(focused_work_item_id or "").strip()
        normalized = normalize_role_runtime_status(status, session.focused_work_item_id)
        if normalized == STATUS_IDLE:
            session.focused_work_item_id = ""
        session.status = normalized
        session.updated_at = datetime.now(UTC)
        return normalized

    def ensure_role_member_session(
        self, role_id: str, employee_id: str = ""
    ) -> CompanyMemberSession:
        """按角色创建/复用成员会话（公司级运行时容纳多角色会话）。"""
        rid = (role_id or "").strip()
        if not rid:
            return self.ensure_member_session()
        emp = (employee_id or "").strip() or rid
        sid = scoped_member_session_id(
            project_id=self.project_id,
            session_scope_id=self.session_scope_id,
            role_id=rid,
            employee_id=emp,
            team_instance_id=self.team_instance_id,
            history_scope_id=self.history_scope_id,
        )
        session = self.state.member_sessions.get(sid)
        if session is None:
            session = self._new_member_session(sid, rid, emp)
        return session

    def _resolve_session_for_role(self, role_id: str) -> CompanyMemberSession:
        if (role_id or "").strip():
            return self.ensure_role_member_session(role_id)
        return self.ensure_member_session()

    def sorted_member_sessions(self) -> list[CompanyMemberSession]:
        # 功能：空闲优先排序的成员会话列表（CM-12 公开化，替代跨模块私有访问 _session_sort_key）
        return sorted(self.state.member_sessions.values(), key=self._session_sort_key)

    def set_member_session_status(
        self,
        session: CompanyMemberSession,
        status: str,
        *,
        focused_work_item_id: str | None = None,
    ) -> str:
        # 功能：归一会话状态（CM-12 公开化，替代跨模块私有访问 _set_member_session_status）
        return self._set_member_session_status(
            session, status, focused_work_item_id=focused_work_item_id
        )

    def is_dispatchable(self, item: WorkItem) -> bool:
        # 功能：判定工作项是否可入队调度（CM-12 公开化，替代跨模块私有访问 _is_dispatchable）
        return self._is_dispatchable(item)

    def _session_id_for_role(self, role_id: str) -> str:
        rid = (role_id or "").strip()
        if not rid:
            return self.member_session_id()
        return scoped_member_session_id(
            project_id=self.project_id,
            session_scope_id=self.session_scope_id,
            role_id=rid,
            employee_id=rid,
            team_instance_id=self.team_instance_id,
            history_scope_id=self.history_scope_id,
        )

    def claim_work_item(self, work_item_id: str, role_id: str = "") -> bool:
        """认领一个工作项：写入 claimed_work_item_ids 并把成员会话置为 running。"""
        if not work_item_id:
            return False
        session = self._resolve_session_for_role(role_id)
        self._set_member_session_status(session, "running", focused_work_item_id=work_item_id)
        session.current_task_id = work_item_id
        session.current_work_item = self._build_current_work_item(session, work_item_id)
        self.state.claimed_work_item_ids.add(work_item_id)
        return True

    def release_work_item(self, work_item_id: str, role_id: str = "") -> bool:
        """释放一个工作项：清出 claimed_work_item_ids 并把成员会话归位 idle。

        返回 False 当 id 为空或该工作项本未被认领（与 claim 空 id 返回 False 语义对称）。
        """
        if not work_item_id:
            return False
        if work_item_id not in self.state.claimed_work_item_ids:
            return False
        self.state.claimed_work_item_ids.discard(work_item_id)
        session = self.state.member_sessions.get(self._session_id_for_role(role_id))
        if session is not None and session.focused_work_item_id == work_item_id:
            self._set_member_session_status(session, "idle")
            session.current_task_id = ""
            session.current_work_item = {}
        return True

    @staticmethod
    def _build_current_work_item(
        session: CompanyMemberSession, work_item_id: str
    ) -> dict[str, Any]:
        """
        类型快照），供会话外查询消费。"""
        return {
            "work_item_id": str(work_item_id or "").strip(),
            "task_id": str(session.current_task_id or "").strip(),
            "role_id": str(session.role_id or "").strip(),
        }

    @staticmethod
    def _session_sort_key(session: CompanyMemberSession) -> tuple[int, str]:
        return (_STATUS_RANK.get(session.status, _UNKNOWN_STATUS_RANK), session.member_session_id)

    def _is_dispatchable(self, item: WorkItem) -> bool:
        if item.phase == Phase.WAITING_DEPENDENCIES:
            return False
        if kanban_column(item.phase) != "todo" or is_terminal(item.phase):
            return False
        if self.dependency_ready is not None:
            from bandi.company.work_item_transition import dependency_work_item_ids

            for dep_id in dependency_work_item_ids(item):
                if not self.dependency_ready(dep_id):
                    return False
        return True

    def _queue_key_for_work_item(self, item: WorkItem) -> str:
        role_id = (item.assignee_role or "").strip()
        team = self.state.home_team_instance_by_role.get(role_id, "")
        return scoped_queue_key(
            session_scope_id=self.session_scope_id, role_id=role_id, team_instance_id=team
        )

    def enqueue_runnable_work_items(self, work_items: list[WorkItem]) -> None:
        for item in work_items:
            if not self._is_dispatchable(item):
                continue
            if item.id in self.state.queued_work_item_ids:
                continue
            queue_key = self._queue_key_for_work_item(item)
            if not queue_key:
                continue
            queue = self.state.role_queues.get(queue_key)
            if queue is None:
                queue = self.state.role_queues[queue_key] = deque()
            tag = f"{_WORK_ITEM_TAG_PREFIX}{item.id}"
            review_tag = f"{_REVIEW_WORK_ITEM_TAG_PREFIX}{item.id}"
            if item.turn_type == WorkItemTurnType.REVIEW:
                queue.appendleft(review_tag)
            else:
                queue.append(tag)
            self.state.queued_work_item_ids.add(item.id)

    def _pop_tag(self, role_id: str, session_scope_id: str | None = None) -> str | None:
        """从指定角色的队首弹出一个工作项 tag，并清出 queued 索引。"""
        scope = (session_scope_id or "").strip() or self.session_scope_id
        team = self.state.home_team_instance_by_role.get(role_id, "")
        queue_key = scoped_queue_key(session_scope_id=scope, role_id=role_id, team_instance_id=team)
        queue = self.state.role_queues.get(queue_key)
        if not queue:
            return None
        while queue:
            tag = queue.popleft()
            if not (
                tag.startswith(_WORK_ITEM_TAG_PREFIX)
                or tag.startswith(_REVIEW_WORK_ITEM_TAG_PREFIX)
            ):
                continue
            work_item_id = tag.split("::", 1)[1]
            self.state.queued_work_item_ids.discard(work_item_id)
            return tag
        return None

    def pop_next_work_item_id(
        self, role_id: str, session_scope_id: str | None = None
    ) -> str | None:
        """从指定角色的队首弹出工作项 id（供调度器 claim 语义使用）。"""
        tag = self._pop_tag(role_id, session_scope_id)
        if tag is None:
            return None
        return tag.split("::", 1)[1]

    def requeue_work_item(self, item: WorkItem) -> None:
        """功能：把已弹出但未派发的工作项放回队尾（幂等；重入不产生重复 tag）"""
        if item.id in self.state.queued_work_item_ids:
            return
        queue_key = self._queue_key_for_work_item(item)
        if not queue_key:
            return
        queue = self.state.role_queues.get(queue_key)
        if queue is None:
            queue = self.state.role_queues[queue_key] = deque()
        queue.append(f"{_WORK_ITEM_TAG_PREFIX}{item.id}")
        self.state.queued_work_item_ids.add(item.id)
