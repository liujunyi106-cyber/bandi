# 功能：工作项规划数据模型——投影优先规格 + 公司运行时计划 + org 蓝图类型（构建逻辑见 org_work_item_planner.py）

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bandi.company._utils import as_int as _as_int
from bandi.company.work_item import normalize_turn_type

# 投影 ID 前缀与 turn 类型字面量（FG-04/FG-14 常量化，避免魔法字符串散落）
CUSTOM_PROJECTION_PREFIX = "custom"
TURN_TYPE_INTAKE = "intake"
TURN_TYPE_DISPATCH = "dispatch"
TURN_TYPE_EXECUTE = "execute"

# 投影 ID 部分之间的分隔符（FG-13 校验投影段不得含此分隔符，防结构污染）
_PROJECTION_SEP = "::"


def _clean_list(values: Any) -> list[str]:
    cleaned = [str(item).strip() for item in list(values or [])]
    return [item for item in cleaned if item]


def _clean_optional(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _as_bool(value: Any) -> bool:
    # 功能：将任意值解析为 bool，字符串 "false"/"0"/"" 判为 False（FG-10 bool("false") 误判）
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


@dataclass
class WorkItemDependencySpec:
    """Dependency from one projected work item to another."""

    projection_id: str
    dependency_projection_id: str
    dependency_class: str = "hard"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "dependency_projection_id": self.dependency_projection_id,
            "dependency_class": self.dependency_class,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkItemDependencySpec:
        payload = dict(data or {})
        return cls(
            projection_id=str(payload.get("projection_id", "") or "").strip(),
            dependency_projection_id=str(payload.get("dependency_projection_id", "") or "").strip(),
            dependency_class=str(payload.get("dependency_class", "") or "hard").strip() or "hard",
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class WorkItemGatePolicy:
    """Projection-first gate policy for company work items."""

    gate_type: str = "review"
    instructions: str = ""
    reviewer_role: str | None = None
    requires_human: bool = False
    on_reject: str = "halt"
    rework_projection_id: str | None = None
    max_retries: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata or {})
        rework_projection_id = str(
            self.metadata.get("rework_projection_id") or self.rework_projection_id or ""
        ).strip()
        self.rework_projection_id = rework_projection_id or None
        self.gate_type = str(self.gate_type or "review").strip().lower() or "review"
        self.on_reject = str(self.on_reject or "halt").strip().lower() or "halt"
        if rework_projection_id:
            self.metadata["rework_projection_id"] = rework_projection_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.gate_type,
            "instructions": self.instructions,
            "reviewer_role": self.reviewer_role,
            "requires_human": bool(self.requires_human),
            "on_reject": self.on_reject,
            "rework_projection_id": self.rework_projection_id,
            "max_retries": int(self.max_retries),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkItemGatePolicy:
        """反序列化 gate 策略；空输入返回默认实例（与其他 from_dict 一致）。

        注意：默认实例代表「未配置 gate」，to_dict 时序列化为 None 保持 round-trip 稳定。
        """
        if not data:
            return cls()
        payload = dict(data or {})
        metadata = dict(payload.get("metadata", {}) or {})
        rework_projection_id = str(
            payload.get("rework_projection_id") or metadata.get("rework_projection_id") or ""
        ).strip()
        return cls(
            gate_type=str(payload.get("type", "") or payload.get("gate_type", "") or "review"),
            instructions=str(payload.get("instructions", "") or ""),
            reviewer_role=str(payload.get("reviewer_role", "") or "").strip() or None,
            requires_human=_as_bool(payload.get("requires_human", False)),
            on_reject=str(payload.get("on_reject", "") or "halt"),
            rework_projection_id=rework_projection_id or None,
            max_retries=_as_int(payload.get("max_retries"), 1),
            metadata=metadata,
        )


@dataclass
class WorkItemReviewPolicy:
    """Review owner policy for a projected work item."""

    review_owner_role_id: str = ""
    review_level: str = "manager"
    max_reworks: int = 10
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_owner_role_id": self.review_owner_role_id,
            "review_level": self.review_level,
            "max_reworks": int(self.max_reworks),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkItemReviewPolicy:
        payload = dict(data or {})
        return cls(
            review_owner_role_id=str(payload.get("review_owner_role_id", "") or "").strip(),
            review_level=str(payload.get("review_level", "") or "manager").strip() or "manager",
            max_reworks=_as_int(payload.get("max_reworks"), 10),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class WorkItemDeliveryPolicy:
    """Delivery policy for a projected company work item."""

    user_visible: bool = False
    authoritative_output: bool = False
    requires_user_feedback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_visible": bool(self.user_visible),
            "authoritative_output": bool(self.authoritative_output),
            "requires_user_feedback": bool(self.requires_user_feedback),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkItemDeliveryPolicy:
        payload = dict(data or {})
        return cls(
            user_visible=_as_bool(payload.get("user_visible", False)),
            authoritative_output=_as_bool(payload.get("authoritative_output", False)),
            requires_user_feedback=_as_bool(payload.get("requires_user_feedback", False)),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


def _normalize_turn_type(value: Any) -> str:
    """Normalize a turn type string to an Bandi enum value, keeping unknown values lowercase."""
    raw = str(value or "execute").strip().lower() or "execute"
    try:
        return normalize_turn_type(raw).value
    except ValueError:
        return raw


@dataclass
class WorkItemProjectionSpec:
    """Projection-first spec consumed by the company work-item runtime."""

    projection_id: str
    turn_type: str
    role_id: str
    title: str
    summary: str = ""
    dependency_projection_ids: list[str] = field(default_factory=list)
    dependency_classes: dict[str, str] = field(default_factory=dict)
    team_id: str = ""
    seat_id: str = ""
    manager_role_id: str = ""
    manager_seat_id: str = ""
    execution_strategy: str = "auto"
    preferred_external_agent: str | None = None
    parallel_group: str | None = None
    prompt_refs: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    handoff_template_ref: str | None = None
    memory_policy_ref: str | None = None
    artifact_contract_ref: str | None = None
    allowed_delegate_role_ids: list[str] = field(default_factory=list)
    contact_role_ids: list[str] = field(default_factory=list)
    gate_policy: WorkItemGatePolicy = field(default_factory=WorkItemGatePolicy)
    review_policy: WorkItemReviewPolicy = field(default_factory=WorkItemReviewPolicy)
    delivery_policy: WorkItemDeliveryPolicy = field(default_factory=WorkItemDeliveryPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "turn_type": self.turn_type,
            "role_id": self.role_id,
            "title": self.title,
            "summary": self.summary,
            "dependency_projection_ids": list(self.dependency_projection_ids),
            "dependency_classes": dict(self.dependency_classes),
            "team_id": self.team_id,
            "seat_id": self.seat_id,
            "manager_role_id": self.manager_role_id,
            "manager_seat_id": self.manager_seat_id,
            "execution_strategy": self.execution_strategy,
            "preferred_external_agent": self.preferred_external_agent,
            "parallel_group": self.parallel_group,
            "prompt_refs": list(self.prompt_refs),
            "skill_refs": list(self.skill_refs),
            "handoff_template_ref": self.handoff_template_ref,
            "memory_policy_ref": self.memory_policy_ref,
            "artifact_contract_ref": self.artifact_contract_ref,
            "allowed_delegate_role_ids": list(self.allowed_delegate_role_ids),
            "contact_role_ids": list(self.contact_role_ids),
            "gate_policy": (
                self.gate_policy.to_dict() if self.gate_policy != WorkItemGatePolicy() else None
            ),
            "review_policy": self.review_policy.to_dict(),
            "delivery_policy": self.delivery_policy.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkItemProjectionSpec:
        payload = dict(data or {})
        projection_id = str(payload.get("projection_id", "") or "").strip()
        return cls(
            projection_id=projection_id,
            turn_type=_normalize_turn_type(payload.get("turn_type")),
            role_id=str(payload.get("role_id", "") or "").strip(),
            title=str(payload.get("title", "") or projection_id or "Work Item").strip(),
            summary=str(payload.get("summary", "") or "").strip(),
            dependency_projection_ids=_clean_list(payload.get("dependency_projection_ids", [])),
            dependency_classes={
                str(key).strip(): str(value).strip()
                for key, value in dict(payload.get("dependency_classes", {}) or {}).items()
                if str(key).strip() and str(value).strip()
            },
            team_id=str(payload.get("team_id", "") or "").strip(),
            seat_id=str(payload.get("seat_id", "") or "").strip(),
            manager_role_id=str(payload.get("manager_role_id", "") or "").strip(),
            manager_seat_id=str(payload.get("manager_seat_id", "") or "").strip(),
            execution_strategy=str(payload.get("execution_strategy", "") or "auto").strip()
            or "auto",
            preferred_external_agent=_clean_optional(payload.get("preferred_external_agent")),
            parallel_group=_clean_optional(payload.get("parallel_group")),
            prompt_refs=_clean_list(payload.get("prompt_refs", [])),
            skill_refs=_clean_list(payload.get("skill_refs", [])),
            handoff_template_ref=_clean_optional(payload.get("handoff_template_ref")),
            memory_policy_ref=_clean_optional(payload.get("memory_policy_ref")),
            artifact_contract_ref=_clean_optional(payload.get("artifact_contract_ref")),
            allowed_delegate_role_ids=_clean_list(payload.get("allowed_delegate_role_ids", [])),
            contact_role_ids=_clean_list(payload.get("contact_role_ids", [])),
            gate_policy=WorkItemGatePolicy.from_dict(payload.get("gate_policy")),
            review_policy=WorkItemReviewPolicy.from_dict(payload.get("review_policy")),
            delivery_policy=WorkItemDeliveryPolicy.from_dict(payload.get("delivery_policy")),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class CompanyWorkItemRuntimePlan:
    """Company mode plan expressed only as projected work items."""

    profile: str = "corporate"
    final_decider_role_id: str = ""
    top_level_role_ids: list[str] = field(default_factory=list)
    root_projection_id: str = ""
    projections: list[WorkItemProjectionSpec] = field(default_factory=list)
    dependencies: list[WorkItemDependencySpec] = field(default_factory=list)
    collaboration_links: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "runtime_model": "multi_team_org",
            "work_item_driven": True,
            "final_decider_role_id": self.final_decider_role_id,
            "top_level_role_ids": list(self.top_level_role_ids),
            "root_projection_id": self.root_projection_id,
            "projections": [projection.to_dict() for projection in self.projections],
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "collaboration_links": [dict(link) for link in self.collaboration_links],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CompanyWorkItemRuntimePlan:
        payload = dict(data or {})
        projections = [
            WorkItemProjectionSpec.from_dict(item)
            for item in list(payload.get("projections", []) or payload.get("seeds", []) or [])
            if isinstance(item, dict)
        ]
        return cls(
            profile=str(payload.get("profile", "") or "corporate").strip() or "corporate",
            final_decider_role_id=str(payload.get("final_decider_role_id", "") or "").strip(),
            top_level_role_ids=_clean_list(payload.get("top_level_role_ids", [])),
            root_projection_id=str(payload.get("root_projection_id", "") or "").strip(),
            projections=projections,
            dependencies=[
                WorkItemDependencySpec.from_dict(item)
                for item in list(payload.get("dependencies", []) or [])
                if isinstance(item, dict)
            ],
            collaboration_links=[
                dict(item)
                for item in list(payload.get("collaboration_links", []) or [])
                if isinstance(item, dict)
            ],
            metadata=dict(payload.get("metadata", {}) or {}),
        )

    def projection_by_id(self) -> dict[str, WorkItemProjectionSpec]:
        return {spec.projection_id: spec for spec in self.projections if spec.projection_id}

    def projection_order_map(self) -> dict[str, int]:
        return {
            spec.projection_id: index
            for index, spec in enumerate(self.projections)
            if spec.projection_id
        }

    def dependencies_for(self, projection_id: str) -> list[str]:
        target = str(projection_id or "").strip()
        for spec in self.projections:
            if spec.projection_id == target:
                return list(spec.dependency_projection_ids)
        return []

    def dependent_projection_ids(self, source_projection_id: str) -> list[str]:
        source = str(source_projection_id or "").strip()
        if not source:
            return []
        return [
            spec.projection_id
            for spec in self.projections
            if spec.projection_id
            and any(
                str(item).strip() == source for item in list(spec.dependency_projection_ids or [])
            )
        ]


def serialize_company_work_item_plan(plan: CompanyWorkItemRuntimePlan | None) -> dict[str, Any]:
    return plan.to_dict() if plan is not None else {}


def deserialize_company_work_item_plan(data: dict[str, Any] | None) -> CompanyWorkItemRuntimePlan:
    return CompanyWorkItemRuntimePlan.from_dict(data)


@dataclass
class OrgWorkItemSeed:
    """A projection-first template for a work item owned by an org seat."""

    projection_id: str
    turn_type: str
    role_id: str
    team_id: str
    seat_id: str
    manager_role_id: str
    title: str
    summary: str
    skill_refs: list[str] = field(default_factory=list)
    prompt_refs: list[str] = field(default_factory=list)
    allowed_delegate_role_ids: list[str] = field(default_factory=list)
    review_owner_role_id: str = ""
    dependency_work_item_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "turn_type": self.turn_type,
            "role_id": self.role_id,
            "team_id": self.team_id,
            "seat_id": self.seat_id,
            "manager_role_id": self.manager_role_id,
            "title": self.title,
            "summary": self.summary,
            "skill_refs": list(self.skill_refs),
            "prompt_refs": list(self.prompt_refs),
            "allowed_delegate_role_ids": list(self.allowed_delegate_role_ids),
            "review_owner_role_id": self.review_owner_role_id,
            "dependency_work_item_ids": list(self.dependency_work_item_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OrgWorkItemSeed:
        payload = dict(data or {})
        return cls(
            projection_id=str(payload.get("projection_id", "") or "").strip(),
            turn_type=_normalize_turn_type(payload.get("turn_type")),
            role_id=str(payload.get("role_id", "") or "").strip(),
            team_id=str(payload.get("team_id", "") or "").strip(),
            seat_id=str(payload.get("seat_id", "") or "").strip(),
            manager_role_id=str(payload.get("manager_role_id", "") or "").strip(),
            title=str(payload.get("title", "") or "").strip(),
            summary=str(payload.get("summary", "") or "").strip(),
            skill_refs=_clean_list(payload.get("skill_refs", [])),
            prompt_refs=_clean_list(payload.get("prompt_refs", [])),
            allowed_delegate_role_ids=_clean_list(payload.get("allowed_delegate_role_ids", [])),
            review_owner_role_id=str(payload.get("review_owner_role_id", "") or "").strip(),
            dependency_work_item_ids=_clean_list(payload.get("dependency_work_item_ids", [])),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass
class OrgWorkItemRuntimeBlueprint:
    """The custom org collaboration plan consumed by the work-item runtime."""

    profile: str = "custom"
    final_decider_role_id: str = ""
    top_level_role_ids: list[str] = field(default_factory=list)
    root_projection_id: str = ""
    seeds: list[OrgWorkItemSeed] = field(default_factory=list)
    collaboration_links: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "runtime_model": "multi_team_org",
            "work_item_driven": True,
            "final_decider_role_id": self.final_decider_role_id,
            "top_level_role_ids": list(self.top_level_role_ids),
            "root_projection_id": self.root_projection_id,
            "seeds": [seed.to_dict() for seed in self.seeds],
            "collaboration_links": [dict(link) for link in self.collaboration_links],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OrgWorkItemRuntimeBlueprint:
        payload = dict(data or {})
        return cls(
            profile=str(payload.get("profile", "") or "custom").strip() or "custom",
            final_decider_role_id=str(payload.get("final_decider_role_id", "") or "").strip(),
            top_level_role_ids=_clean_list(payload.get("top_level_role_ids", [])),
            root_projection_id=str(payload.get("root_projection_id", "") or "").strip(),
            seeds=[
                OrgWorkItemSeed.from_dict(item)
                for item in list(payload.get("seeds", []) or [])
                if isinstance(item, dict)
            ],
            collaboration_links=[
                dict(item)
                for item in list(payload.get("collaboration_links", []) or [])
                if isinstance(item, dict)
            ],
            metadata=dict(payload.get("metadata", {}) or {}),
        )
