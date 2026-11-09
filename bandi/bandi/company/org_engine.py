# 功能：组织引擎——角色/成员/层级/汇报链/chain_of_command

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bandi.company.reorg_manager import OrgSnapshot

logger = logging.getLogger(__name__)

_PERSIST_DEGRADED_THRESHOLD = 3


@dataclass
class OrgRole:
    id: str
    name: str
    reports_to: str | None = None
    can_spawn: list[str] = field(default_factory=list)
    permissions: dict[str, list[str]] = field(default_factory=dict)
    # （进 work item summary / 上下文渲染）
    responsibility: str = ""
    tool_tags: list[str] = field(default_factory=list)
    # 而非依赖角色注册插入顺序的 tops[0]（注册顺序变化不再改变决策者）
    is_final_decider: bool = False
    # 多团队互 route：角色归属团队（home team）——调度队列/会话按 team 隔离
    team_id: str = ""
    # .6：角色级落盘目录白名单（相对=项目根基准；空=不过滤）
    fs_scopes: list[str] = field(default_factory=list)

    @property
    def role_id(self) -> str:
        return self.id


@dataclass
class OrgMember:
    user_id: str
    role_id: str
    name: str = ""


@dataclass
class OrgEmployee:
    employee_id: str
    name: str
    role_id: str
    template_id: str = ""
    description: str = ""
    category: str = ""
    domains: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    status: str = "active"
    llm_config: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "name": self.name,
            "role_id": self.role_id,
            "template_id": self.template_id,
            "description": self.description,
            "category": self.category,
            "domains": list(self.domains),
            "metadata": dict(self.metadata),
            "status": self.status,
            "llm_config": self.llm_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrgEmployee:
        return cls(
            employee_id=data["employee_id"],
            name=data["name"],
            role_id=data["role_id"],
            template_id=data.get("template_id", ""),
            description=data.get("description", ""),
            category=data.get("category", ""),
            domains=list(data.get("domains", [])),
            metadata=data.get("metadata", {}),
            status=data.get("status", "active"),
            llm_config=data.get("llm_config"),
        )


_IDENTITY_RESPONSIBILITY_CAP = 500
_IDENTITY_PERSONA_CAP = 400
_IDENTITY_SUBORDINATE_LIMIT = 10


class OrgEngine:
    def __init__(
        self, persist_store: Any = None, persist_callable: Callable[[], None] | None = None
    ) -> None:
        self._roles: dict[str, OrgRole] = {}
        self._members: dict[str, OrgMember] = {}
        self._employees: dict[str, OrgEmployee] = {}
        self._org_version = 1
        self._runtime_topology_version = 1
        self._company_profile = "corporate"
        self.installed_packages: list[Any] = []
        self._persist_store = persist_store
        # 磁盘未落 → 重启回退分裂，必须告警并提示人工落盘）。
        # persist_callable：org.json 通道（CLI/API 装配传 save_org），无 store 时
        # persist_health 恒健康（degraded 告警从未在生产生效）。
        self._persist_callable = persist_callable
        self._persist_failed_count = 0
        self._persist_last_error: str = ""
        self._persist_degraded = False
        # 功能：加载基线快照（角色 id 集合）+ 显式墓碑集合——写前重读合并据此判定
        # 本进程主动删除的角色，防「删除被盘上旧数据复活」。基线覆盖整表替换路径
        # （apply_changeset/restore），墓碑覆盖未加载进程的直接 remove_role。
        self._baseline_role_ids: set[str] = set()
        self._removed_role_ids: set[str] = set()
        self._deleted_employee_ids: set[str] = set()
        self._load_builtin()
        if persist_store is not None:
            self.restore(persist_store)

    def snapshot_baseline(self) -> None:
        """功能：记录当前角色集合为本进程基线（load_org 加载完成后调用）。"""
        self._baseline_role_ids = set(self._roles)

    @property
    def deleted_employee_ids(self) -> frozenset[str]:
        """功能：本进程主动删除的员工 ID（写前合并不得从盘上复活）。"""
        return frozenset(self._deleted_employee_ids)

    @property
    def deleted_role_ids(self) -> frozenset[str]:
        """功能：相对基线被删除、或经 remove_role 显式删除的角色 id（重新注册即解除）
        ——写前合并须跳过，防复活删除。"""
        current = set(self._roles)
        return frozenset((self._baseline_role_ids | self._removed_role_ids) - current)

    def _load_builtin(self) -> None:
        builtin = {
            "ceo": OrgRole("ceo", "CEO", reports_to=None, can_spawn=["cto", "cfo"]),
            "cto": OrgRole("cto", "CTO", reports_to="ceo", can_spawn=["em", "architect"]),
            "cfo": OrgRole("cfo", "CFO", reports_to="ceo", can_spawn=["finance"]),
            "em": OrgRole("em", "Engineering Manager", reports_to="cto", can_spawn=["engineer"]),
            "architect": OrgRole("architect", "Architect", reports_to="cto", can_spawn=[]),
            "engineer": OrgRole("engineer", "Engineer", reports_to="em", can_spawn=[]),
            "analyst": OrgRole("analyst", "Analyst", reports_to="cto", can_spawn=[]),
            "finance": OrgRole("finance", "Finance", reports_to="cfo", can_spawn=[]),
            "viewer": OrgRole("viewer", "Viewer", reports_to=None, can_spawn=[]),
        }
        self._roles.update(builtin)

    @staticmethod
    def _find_cycle(roles: dict[str, OrgRole], start: str) -> str | None:
        """沿 reports_to 链上行检测环，返回环上角色 id；无环返回 None。"""
        seen: set[str] = set()
        current: str | None = start
        while current:
            if current in seen:
                return current
            seen.add(current)
            parent = roles.get(current)
            current = parent.reports_to if parent else None
        return None

    def register_role(
        self,
        role_id: str,
        name: str,
        responsibility: str = "",
        reports_to: str | None = None,
        can_spawn: list[str] | None = None,
        permissions: dict[str, Any] | None = None,
        tool_tags: list[str] | None = None,
        is_final_decider: bool = False,
        team_id: str = "",
        fs_scopes: list[str] | None = None,
    ) -> OrgRole:
        if reports_to:
            if reports_to == role_id:
                raise ValueError(f"Role `{role_id}` cannot report to itself.")
            if reports_to not in self._roles:
                raise ValueError(f"Unknown reports_to role `{reports_to}` for `{role_id}`.")
        role = OrgRole(
            id=role_id,
            name=name,
            reports_to=reports_to,
            can_spawn=can_spawn or [],
            permissions=dict(permissions or {}),
            responsibility=responsibility,
            tool_tags=list(tool_tags or []),
            is_final_decider=is_final_decider,
            team_id=team_id,
            fs_scopes=list(fs_scopes or []),
        )
        if reports_to:
            candidate = {**self._roles, role_id: role}
            cycle_at = self._find_cycle(candidate, role_id)
            if cycle_at is not None:
                raise ValueError(f"reports_to cycle detected for role `{role_id}`.")
        self._roles[role_id] = role
        self._persist()
        return role

    def get_role(self, role_id: str) -> OrgRole | None:
        return self._roles.get(role_id)

    def set_reports_to(self, role_id: str, reports_to: str) -> None:
        """深度复审修复：加载后补设角色上级（org.json 两遍加载）——未知上级抛
        ValueError（不静默）；自引用/环由 _find_cycle 校验。"""
        role = self._roles.get(role_id)
        if role is None:
            raise ValueError(f"Unknown role `{role_id}`.")
        if reports_to == role_id:
            raise ValueError(f"Role `{role_id}` cannot report to itself.")
        if reports_to not in self._roles:
            raise ValueError(f"Unknown reports_to role `{reports_to}` for `{role_id}`.")
        candidate = {
            **self._roles,
            role_id: OrgRole(
                id=role.id,
                name=role.name,
                reports_to=reports_to,
                can_spawn=list(role.can_spawn),
                permissions=dict(role.permissions),
                responsibility=role.responsibility,
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id=role.team_id,
                fs_scopes=list(role.fs_scopes),
            ),
        }
        if self._find_cycle(candidate, role_id):
            raise ValueError(f"Role `{role_id}` reports_to `{reports_to}` creates a cycle.")
        self._roles[role_id] = candidate[role_id]
        self._org_version += 1
        self._persist()

    def get_agent(self, role_id: str) -> OrgRole | None:
        return self.get_role(role_id)

    def get_executor(self, role_id: str) -> OrgRole | None:
        return self.get_role(role_id)

    def list_roles(self) -> list[OrgRole]:
        return list(self._roles.values())

    def list_agents(self) -> list[OrgRole]:
        return list(self._roles.values())

    def get_top_level_role_ids(self) -> list[str]:
        return [r for r, a in self._roles.items() if a.reports_to is None]

    def get_final_decider_role_id(self) -> str | None:
        # 无显式标记时回退 tops[0]（向后兼容旧行为）。
        for role in self._roles.values():
            if role.is_final_decider:
                return role.id
        tops = self.get_top_level_role_ids()
        return tops[0] if tops else None

    def get_coordinator(self) -> OrgRole | None:
        top = self.get_final_decider_role_id()
        if top is None:
            return None
        return self._roles.get(top)

    def get_chain_of_command(self, role_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current = role_id
        while current:
            if current in seen:
                raise ValueError(f"reports_to cycle detected at role `{current}`.")
            seen.add(current)
            role = self._roles.get(current)
            if not role:
                break
            chain.append(current)
            current = role.reports_to
        return chain

    def can_spawn(self, caller_role: str, target_role: str) -> bool:
        role = self._roles.get(caller_role)
        return role is not None and target_role in role.can_spawn

    def get_allowed_contact_roles(self, role_id: str) -> list[str]:
        role = self._roles.get(role_id)
        if role is None:
            return []
        allowed: list[str] = []
        if role.reports_to:
            allowed.append(role.reports_to)
        for r in self._roles.values():
            if r.reports_to == role_id:
                allowed.append(r.id)
        return allowed

    def register_member(self, member: OrgMember) -> None:
        self._members[member.user_id] = member

    def get_member(self, user_id: str) -> OrgMember | None:
        return self._members.get(user_id)

    def list_members(self) -> list[OrgMember]:
        return list(self._members.values())

    def register_employee(self, employee: OrgEmployee) -> None:
        self._employees[employee.employee_id] = employee
        self._persist()

    def get_employee(self, employee_id: str) -> OrgEmployee | None:
        return self._employees.get(employee_id)

    def remove_employee(self, employee_id: str) -> bool:
        """功能：移除员工记录（不存在返回 False）。"""
        if employee_id not in self._employees:
            return False
        self._employees.pop(employee_id, None)
        self._deleted_employee_ids.add(employee_id)
        self._org_version += 1
        self._persist()
        return True

    def list_employees(
        self, role_id: str | None = None, include_inactive: bool = False
    ) -> list[OrgEmployee]:
        employees = [
            e for e in self._employees.values() if include_inactive or e.status == "active"
        ]
        if role_id is None:
            return employees
        return [e for e in employees if e.role_id == role_id]

    def reallocate_employee_roles(
        self, role_mapping: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        migrated: list[str] = []
        retired: list[str] = []
        for employee in list(self._employees.values()):
            new_role = role_mapping.get(employee.role_id)
            if new_role is None:
                continue
            if new_role:
                employee.role_id = new_role
                migrated.append(employee.employee_id)
            else:
                employee.role_id = ""
                employee.status = "retired"
                retired.append(employee.employee_id)
        return migrated, retired

    def employee_role_ids(self, employee: OrgEmployee) -> list[str]:
        extra = employee.metadata.get("role_ids", [])
        extras = [r for r in extra if isinstance(r, str)]
        return [employee.role_id] + [r for r in extras if r not in (employee.role_id,)]

    def get_role_prompt_context(self, role_id: str, employee_id: str = "") -> str:
        """功能：渲染员工执行身份段（断链接线）——职责/汇报关系/员工名/persona
        经 seat_executor 写入 runtime.metadata，由 CompanyContextSource 注入 system
        prompt。角色不存在返回空串；自引用脏数据（绕过 register_role 注入）只做
        单层查找不会死循环。"""
        role = self._roles.get(role_id)
        if role is None:
            return ""
        subs = [r.name or r.id for r in self._roles.values() if r.reports_to == role_id][
            :_IDENTITY_SUBORDINATE_LIMIT
        ]
        parent = self._roles.get(role.reports_to) if role.reports_to else None
        lines = [
            "你是数字员工组织中的正式成员，须始终以本岗位职责口径执行任务。",
            f"角色：{role.name}（{role.id}）",
        ]
        if role.responsibility:
            lines.append(f"职责：{role.responsibility[:_IDENTITY_RESPONSIBILITY_CAP]}")
        if parent is not None:
            lines.append(f"汇报对象：{parent.name}（{parent.id}）")
        if subs:
            lines.append(f"协作下属：{'、'.join(subs)}")
        employee = self._employees.get(employee_id) if employee_id else None
        if employee is not None:
            lines.append(f"员工：{employee.name}（{employee.employee_id}）")
            persona = str(employee.metadata.get("persona_prompt") or "").strip()
            if persona:
                lines.append(f"员工角色补充要求：{persona[:_IDENTITY_PERSONA_CAP]}")
        return "\n".join(lines)

    def current_org_version(self) -> int:
        return self._org_version

    def current_runtime_topology_version(self) -> int:
        return self._runtime_topology_version

    def get_company_profile(self) -> str:
        return self._company_profile

    def _persist(self) -> None:
        """内部：变更后写入 store / org.json 回调（两者皆无则静默跳过）。"""
        if self._persist_store is None and self._persist_callable is None:
            return
        try:
            if self._persist_store is not None:
                self.persist(self._persist_store)
            elif self._persist_callable is not None:
                self._persist_callable()
            self._persist_failed_count = 0
            self._persist_last_error = ""
            self._persist_degraded = False
        except Exception as e:  # noqa: BLE001 — 持久化失败不阻断业务，但必须显式告警
            self._persist_failed_count += 1
            self._persist_last_error = f"{type(e).__name__}: {e}"
            # 连续失败达阈值标记 degraded——内存组织与磁盘分裂，重启回退风险
            if self._persist_failed_count >= _PERSIST_DEGRADED_THRESHOLD:
                self._persist_degraded = True
            logger.error(
                "org persist failed (count=%d): %s",
                self._persist_failed_count,
                self._persist_last_error,
            )

    def persist_health(self) -> dict[str, Any]:
        """持久化健康状态——供 dispatcher/UI/审计告警消费。"""
        return {
            "healthy": self._persist_failed_count == 0,
            "degraded": self._persist_degraded,
            "failed_count": self._persist_failed_count,
            "last_error": self._persist_last_error,
        }

    def snapshot_org(
        self,
        project_id: str = "default",
        active_tasks: list[dict[str, Any]] | None = None,
    ) -> OrgSnapshot:
        from bandi.company.reorg_manager import OrgSnapshot

        return OrgSnapshot(
            project_id=project_id,
            org_version=self._org_version,
            runtime_topology_version=self._runtime_topology_version,
            company_name="",
            topology="",
            roles=[self._role_to_dict(role) for role in self.list_agents()],
            company_profile=self._company_profile,
            active_tasks=list(active_tasks or []),
            metadata={},
        )

    # ── 持久化 ────────────────────────────────────────────────────────

    def persist(self, store: Any) -> None:
        """将当前角色/员工状态写入 store（供 CompanyDispatcher 调用）。

        =A：本通道为**非生产**组织持久化（生产单源 = org.json，见
        org_persistence.py）；保留供测试与既有装配。员工序列化复用
        OrgEmployee.to_dict()（单一来源，含 llm_config——手写 dict 曾漏致换大脑断链）。
        """
        roles_data = [self._role_to_dict(r) for r in self._roles.values()]
        employees_data = [e.to_dict() for e in self._employees.values()]
        store.save_org_roles(roles_data)
        store.save_org_employees(employees_data)

    def restore(self, store: Any) -> None:
        """从 store 恢复角色/员工状态（供 CompanyDispatcher 初始化时调用）。

        非生产通道（见 persist）；org.json 生产恢复走 org_persistence.load_org。
        """
        roles_data = store.load_org_roles()
        employees_data = store.load_org_employees()
        # 先清空（保留 builtin）
        self._roles.clear()
        self._employees.clear()
        self._load_builtin()
        # 覆盖内置角色（持久化的自定义角色优先）
        for r in roles_data:
            role = OrgRole(
                id=r["role_id"],
                name=r["name"],
                reports_to=r.get("reports_to"),
                can_spawn=r.get("can_spawn", []),
                permissions=r.get("permissions", {}),
                responsibility=r.get("responsibility", ""),
                tool_tags=list(r.get("tool_tags", []) or []),
                is_final_decider=bool(r.get("is_final_decider", False)),
                team_id=str(r.get("team_id") or ""),
                fs_scopes=list(r.get("fs_scopes", []) or []),
            )
            self._roles[role.id] = role
        for e in employees_data:
            emp = OrgEmployee(
                employee_id=e["employee_id"],
                name=e["name"],
                role_id=e["role_id"],
                template_id=e.get("template_id", ""),
                description=e.get("description", ""),
                category=e.get("category", ""),
                domains=list(e.get("domains", [])),
                metadata=e.get("metadata", {}),
                status=e.get("status", "active"),
                llm_config=e.get("llm_config"),
            )
            self._employees[emp.employee_id] = emp
        if roles_data or employees_data:
            self._org_version += 1

    @staticmethod
    def _role_to_dict(role: OrgRole) -> dict[str, Any]:
        return {
            "role_id": role.id,
            "name": role.name,
            "reports_to": role.reports_to,
            "can_spawn": list(role.can_spawn),
            "permissions": dict(role.permissions),
            # org.json 重载后 work item summary 丢角色职责
            "responsibility": role.responsibility,
            "tool_tags": list(role.tool_tags),
            "is_final_decider": role.is_final_decider,
            "team_id": role.team_id,
            "fs_scopes": list(role.fs_scopes),
        }

    @classmethod
    def _role_from_dict(cls, payload: dict[str, Any]) -> OrgRole:
        return OrgRole(
            # 双键兼容：_role_to_dict 输出 role_id 键、org.json 用 id 键
            # 反序列化必须同时接受两种形态，防止直连调用键名错位清空 id
            id=str(payload.get("id") or payload.get("role_id") or ""),
            name=str(payload.get("name") or payload.get("id") or ""),
            reports_to=payload.get("reports_to"),
            can_spawn=list(payload.get("can_spawn", []) or []),
            permissions=dict(payload.get("permissions", {}) or {}),
            responsibility=str(payload.get("responsibility") or ""),
            tool_tags=list(payload.get("tool_tags", []) or []),
            is_final_decider=bool(payload.get("is_final_decider", False)),
            team_id=str(payload.get("team_id") or ""),
            fs_scopes=list(payload.get("fs_scopes", []) or []),
        )

    def validate_changeset(self, changeset: Any) -> list[str]:
        errors: list[str] = []
        role_ids = set(self._roles.keys())
        for change in changeset.role_changes:
            if change.action in {"remove", "replace", "update"} and change.role_id not in role_ids:
                errors.append(f"Unknown role `{change.role_id}` for action `{change.action}`.")
            if change.action in {"add", "replace"}:
                # 对 None 抛 AttributeError（脏数据崩溃）
                role_payload = change.role if isinstance(change.role, dict) else {}
                target = change.replacement_role_id or role_payload.get("id", "")
                if not target:
                    errors.append(f"Role change `{change.action}` requires a new role id.")
        return errors

    def add_role(self, role: OrgRole) -> None:
        self._roles[role.id] = role
        self._org_version += 1
        self._persist()

    def remove_role(self, role_id: str) -> None:
        self._roles.pop(role_id, None)
        self._removed_role_ids.add(role_id)  # 墓碑：写前合并不得把该角色从盘中复活
        self._org_version += 1
        self._persist()

    def apply_changeset(self, changeset: Any, persist: bool = True) -> dict[str, Any]:
        errors = self.validate_changeset(changeset)
        if errors:
            raise ValueError("; ".join(errors))

        old_org_version = self._org_version
        roles = {role_id: role for role_id, role in self._roles.items()}
        role_mapping: dict[str, str] = {}

        for change in changeset.role_changes:
            role_payload = dict(change.role) if isinstance(change.role, dict) else {}
            target_role_id = change.replacement_role_id or role_payload.get("id") or change.role_id
            if change.action == "add":
                role_payload.setdefault("id", target_role_id)
                roles[target_role_id] = self._role_from_dict(role_payload)
            elif change.action == "remove":
                roles.pop(change.role_id, None)
                role_mapping[change.role_id] = (
                    target_role_id if target_role_id != change.role_id else ""
                )
            elif change.action == "replace":
                roles.pop(change.role_id, None)
                if role_payload:
                    role_payload.setdefault("id", target_role_id)
                    roles[target_role_id] = self._role_from_dict(role_payload)
                role_mapping[change.role_id] = target_role_id
            elif change.action == "update":
                current = roles.get(change.role_id)
                if current is None:
                    continue
                merged = self._role_to_dict(current)
                merged.update(role_payload)
                # update 是原位更新：id 恒等于原角色 id（改 id 应走 replace）
                merged["id"] = change.role_id
                roles[change.role_id] = self._role_from_dict(merged)

        for role_id in roles:
            cycle_at = self._find_cycle(roles, role_id)
            if cycle_at is not None:
                raise ValueError(f"reports_to cycle detected in changeset at role `{cycle_at}`.")
        self._roles = roles
        if self._company_profile == "corporate" and changeset.role_changes:
            self._company_profile = "custom"
        self._org_version += 1
        # 重启/换进程读取回退（与 register_role/remove_role 等路径语义一致）
        if persist:
            self._persist()
        return {
            "old_org_version": old_org_version,
            "new_org_version": self._org_version,
            "role_mapping": role_mapping,
        }
