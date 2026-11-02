# 功能：招募系统——LLM CompanyRecruiter + 人才市场对接（座位调度见 seat_recruiter.py）

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from bandi.company.seat_recruiter import (
    AgentSeat,  # noqa: F401 - re-export（外部从 bandi.company.recruiter 引入座位调度）
    Recruiter,  # noqa: F401 - re-export
    RecruitStatus,  # noqa: F401 - re-export
)
from bandi.utils.llm_json import parse_llm_json_strict

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

ROOT_MANAGER_ROLE_ID = "owner"
TASK_GENERALIST_ROLE_ID = "task_generalist"


def _as_float(value: Any, default: float = 0.0) -> float:
    # 功能：将任意值解析为 float，非数字输入回退默认值（float 转换兜底）
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 魔法数字常量化：反馈上限 / 简单请求文本长度阈值 / 分类数上限 / 分类权重
_FEEDBACK_MAX_ITEMS = 5
_TRIVIAL_REQUEST_MAX_LEN = 24
_MAX_SELECTED_CATEGORIES = 3
_CATEGORY_TEMPLATE_BONUS_CAP = 3
_CATEGORY_TEMPLATE_BONUS_WEIGHT = 0.1
_MAX_FALLBACK_CATEGORIES = 2

GLOBAL_RECRUITER_PROMPT = """\
You are the staffing recruiter for a company before it starts execution.

Your job is to produce one global staffing plan for all roles at once. Compare:
- each role's responsibility and the user's request
- the shared top-level employee_pool of existing company employees with experience
- the shared top-level candidate_pool of imported talent templates
- org_graph, the reporting/delegation structure between roles
- recruiter feedback from earlier revisions

Return strict JSON:
{
  "proposals": [
    {
      "role_id": "role id from the payload",
      "status": "existing_staff" | "proposed_hire" | "fallback_role_only" | "direct_role_execution",
      "employee_id": "existing employee id or empty string",
      "template_id": "candidate template id or empty string",
      "proposed_employee_name": "short employee display name or empty string",
      "rationale": "brief reason"
    }
  ]
}

Rules:
- Return exactly one proposal for every role in the payload and no extra roles.
- Consider both the user's request and every role's role_responsibility.
- Use org_graph as the reporting/delegation structure. Managers may coordinate or cover adjacent work; leaf roles usually represent dedicated execution/review specialties. Reuse the same employee/template across roles only when this structure and role responsibilities make shared coverage sensible; explain why.
- If you repeat the same employee or template across roles, explain why it still fits each role in that role's rationale.
- selected_categories are role-level hints from triage; employee_pool and candidate_pool are shared across all roles.
- Only choose employee_id values from the top-level employee_pool and template_id values from the top-level candidate_pool.
- Choosing employee_id means use the existing experienced employee. Choosing template_id means use the template-only version for this run.
- Use `direct_role_execution` when ordinary role execution is enough and no staffing decision is useful.
- Use `fallback_role_only` when the visible candidates and staff are not credible for the role.
- Respect recruiter feedback if present.
- Return JSON only.
"""

STAFFING_TRIAGE_PROMPT = """\
You are the staffing recruiter for a company before it starts execution.

First decide which roles need deliberate staffing and which talent categories should be considered.

Return strict JSON:
{
  "roles": [
    {
      "role_id": "role id from the payload",
      "action": "direct_role_execution" | "category_screening",
      "categories": ["category-1", "category-2"],
      "rationale": "brief reason"
    }
  ]
}

Rules:
- Return exactly one entry for every role in the payload and no extra roles.
- Use the user's request as the primary signal, then map useful categories to roles with role responsibilities and org_graph.
- Generic or short role responsibilities do not by themselves mean staffing is unnecessary.
- Choose `direct_role_execution` only when no staffing comparison is useful for that role.
- Choose `category_screening` when the role could benefit from a specialized employee/template for this request.
- If action is `direct_role_execution`, return an empty categories list.
- If action is `category_screening`, select 1 to 3 categories only.
- Only choose categories from the provided category catalog.
- Use org_graph as the reporting/delegation structure.
- Respect recruiter feedback if present.
- Return JSON only.
"""

RECRUITMENT_AGENT_CHOICES = frozenset({"native", "codex", "claude_code", "cursor", "opencode"})
DEFAULT_RECRUITMENT_EXECUTION_AGENT = "codex"
VALID_RECRUITMENT_STATUSES = frozenset(
    {"existing_staff", "proposed_hire", "fallback_role_only", "direct_role_execution"}
)
_RECRUITER_MAX_RETRIES = 3


@dataclass
class RecruitmentNeed:
    role_id: str
    role_name: str = ""
    role_responsibility: str = ""
    request_text: str = ""
    domains: list[str] = field(default_factory=list)
    existing_employee_ids: list[str] = field(default_factory=list)


@dataclass
class RecruitmentCandidateRecommendation:
    template_id: str
    template_name: str
    category: str = ""
    domains: list[str] = field(default_factory=list)
    prompt_ref: str = ""
    preferred_external_agent: str | None = None
    source_path: str = ""
    rationale: str = ""
    proposed_employee_name: str = ""
    proposed_employee_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentEmployeeRecommendation:
    employee_id: str
    employee_name: str
    role_id: str
    category: str = ""
    domains: list[str] = field(default_factory=list)
    learned_skill_refs: list[str] = field(default_factory=list)
    experience_score: float = 0.0
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentProposal:
    role_id: str
    status: Literal[
        "existing_staff", "proposed_hire", "fallback_role_only", "direct_role_execution"
    ] = "fallback_role_only"
    rationale: str = ""
    role_labels: list[str] = field(default_factory=list)
    candidate: RecruitmentCandidateRecommendation | None = None
    existing_employee: RecruitmentEmployeeRecommendation | None = None
    existing_employee_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentPlan:
    company_profile: str = "corporate"
    proposals: list[RecruitmentProposal] = field(default_factory=list)
    recruiter_feedback: list[str] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_recruitment_agent_choice(value: Any, default: str | None = None) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in RECRUITMENT_AGENT_CHOICES:
        return normalized
    fallback = str(default or "").strip().lower().replace("-", "_")
    if fallback in RECRUITMENT_AGENT_CHOICES:
        return fallback
    return None


def resolve_effective_execution_agent(
    selected_agent: Any,
    preferred_external_agent: Any = None,
    *,
    force_native_execution: bool = False,
) -> tuple[str, str | None, bool]:
    preferred = normalize_recruitment_agent_choice(preferred_external_agent)
    if preferred == "native":
        preferred = None
    selected = normalize_recruitment_agent_choice(
        selected_agent,
        default=("native" if not preferred else preferred),
    )
    resolved_force_native = bool(force_native_execution or selected == "native")
    if resolved_force_native:
        return "native", None, True
    assigned_external = selected if selected and selected != "native" else preferred
    return selected or assigned_external or "native", assigned_external, False


def ensure_recruitment_plan_default_agents(
    plan: RecruitmentPlan,
    *,
    default_agent: str = DEFAULT_RECRUITMENT_EXECUTION_AGENT,
) -> RecruitmentPlan:
    normalized_default = (
        normalize_recruitment_agent_choice(
            default_agent,
            default=DEFAULT_RECRUITMENT_EXECUTION_AGENT,
        )
        or DEFAULT_RECRUITMENT_EXECUTION_AGENT
    )
    for proposal in plan.proposals:
        metadata = dict(proposal.metadata or {})
        metadata["selected_execution_agent"] = (
            normalize_recruitment_agent_choice(metadata.get("selected_execution_agent"))
            or normalized_default
        )
        proposal.metadata = metadata
    return plan


def apply_recruitment_role_agent_overrides(
    plan: RecruitmentPlan,
    role_agent_overrides: dict[str, Any] | None,
) -> None:
    if not role_agent_overrides:
        return
    normalized_overrides: dict[str, str] = {}
    for raw_role_id, raw_agent in dict(role_agent_overrides).items():
        role_id = str(raw_role_id or "").strip()
        agent = normalize_recruitment_agent_choice(raw_agent)
        if role_id and agent:
            normalized_overrides[role_id] = agent
    if not normalized_overrides:
        return
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        selected_agent = normalized_overrides.get(role_id)
        if not selected_agent:
            continue
        proposal.metadata = dict(proposal.metadata)
        proposal.metadata["selected_execution_agent"] = selected_agent


def extract_recruitment_role_agent_overrides(plan: RecruitmentPlan) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        if not role_id:
            continue
        selected_agent = normalize_recruitment_agent_choice(
            dict(proposal.metadata or {}).get("selected_execution_agent")
        )
        if selected_agent:
            overrides[role_id] = selected_agent
    return overrides


def build_staffing_overrides(plan: RecruitmentPlan) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for proposal in plan.proposals:
        if proposal.status == "existing_staff" and proposal.existing_employee:
            overrides[proposal.role_id] = proposal.existing_employee.employee_id
        elif (
            proposal.status == "proposed_hire"
            and proposal.candidate
            and proposal.candidate.proposed_employee_id
        ):
            overrides[proposal.role_id] = proposal.candidate.proposed_employee_id
    return overrides


def build_staffing_experience_modes(plan: RecruitmentPlan) -> dict[str, str]:
    modes: dict[str, str] = {}
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        if not role_id:
            continue
        if proposal.status == "existing_staff" and proposal.existing_employee:
            modes[role_id] = "with_experience"
        elif proposal.status == "proposed_hire" and proposal.candidate:
            modes[role_id] = "template_only"
    return modes


def build_fallback_role_ids(plan: RecruitmentPlan) -> set[str]:
    return {
        str(proposal.role_id).strip()
        for proposal in plan.proposals
        if proposal.status == "fallback_role_only" and str(proposal.role_id).strip()
    }


def recruitment_plan_requires_confirmation(plan: RecruitmentPlan) -> bool:
    return any(
        proposal.status in {"existing_staff", "proposed_hire"} for proposal in plan.proposals
    )


def serialize_recruitment_plan(plan: RecruitmentPlan) -> dict[str, Any]:
    return {
        "company_profile": plan.company_profile,
        "proposals": [
            {
                "role_id": proposal.role_id,
                "status": proposal.status,
                "rationale": proposal.rationale,
                "role_labels": list(proposal.role_labels),
                "candidate": (
                    {
                        "template_id": proposal.candidate.template_id,
                        "template_name": proposal.candidate.template_name,
                        "category": proposal.candidate.category,
                        "domains": list(proposal.candidate.domains),
                        "prompt_ref": proposal.candidate.prompt_ref,
                        "preferred_external_agent": proposal.candidate.preferred_external_agent,
                        "source_path": proposal.candidate.source_path,
                        "rationale": proposal.candidate.rationale,
                        "proposed_employee_name": proposal.candidate.proposed_employee_name,
                        "proposed_employee_id": proposal.candidate.proposed_employee_id,
                        "metadata": dict(proposal.candidate.metadata),
                    }
                    if proposal.candidate
                    else None
                ),
                "existing_employee": (
                    {
                        "employee_id": proposal.existing_employee.employee_id,
                        "employee_name": proposal.existing_employee.employee_name,
                        "role_id": proposal.existing_employee.role_id,
                        "category": proposal.existing_employee.category,
                        "domains": list(proposal.existing_employee.domains),
                        "learned_skill_refs": list(proposal.existing_employee.learned_skill_refs),
                        "experience_score": proposal.existing_employee.experience_score,
                        "rationale": proposal.existing_employee.rationale,
                        "metadata": dict(proposal.existing_employee.metadata),
                    }
                    if proposal.existing_employee
                    else None
                ),
                "existing_employee_ids": list(proposal.existing_employee_ids),
                "metadata": dict(proposal.metadata),
            }
            for proposal in plan.proposals
        ],
        "recruiter_feedback": list(plan.recruiter_feedback),
        "summary": plan.summary,
        "metadata": dict(plan.metadata),
    }


def deserialize_recruitment_plan(data: dict[str, Any]) -> RecruitmentPlan:
    proposals: list[RecruitmentProposal] = []
    for item in data.get("proposals", []):
        candidate_data = item.get("candidate")
        existing_employee_data = item.get("existing_employee")
        candidate = None
        existing_employee = None
        if candidate_data:
            candidate = RecruitmentCandidateRecommendation(
                template_id=str(candidate_data.get("template_id", "")),
                template_name=str(candidate_data.get("template_name", "")),
                category=str(candidate_data.get("category", "")),
                domains=list(candidate_data.get("domains", [])),
                prompt_ref=str(candidate_data.get("prompt_ref", "")),
                preferred_external_agent=candidate_data.get("preferred_external_agent"),
                source_path=str(candidate_data.get("source_path", "")),
                rationale=str(candidate_data.get("rationale", "")),
                proposed_employee_name=str(candidate_data.get("proposed_employee_name", "")),
                proposed_employee_id=str(candidate_data.get("proposed_employee_id", "")),
                metadata=dict(candidate_data.get("metadata", {})),
            )
        if existing_employee_data:
            existing_employee = RecruitmentEmployeeRecommendation(
                employee_id=str(existing_employee_data.get("employee_id", "")),
                employee_name=str(existing_employee_data.get("employee_name", "")),
                role_id=str(existing_employee_data.get("role_id", "")),
                category=str(existing_employee_data.get("category", "")),
                domains=list(existing_employee_data.get("domains", [])),
                learned_skill_refs=list(existing_employee_data.get("learned_skill_refs", [])),
                experience_score=_as_float(existing_employee_data.get("experience_score")),
                rationale=str(existing_employee_data.get("rationale", "")),
                metadata=dict(existing_employee_data.get("metadata", {})),
            )
        status = str(item.get("status", "fallback_role_only") or "").strip().lower()
        if status not in VALID_RECRUITMENT_STATUSES:
            raise ValueError(
                f"Invalid status `{status}`. "
                f"Choose one of: {', '.join(sorted(VALID_RECRUITMENT_STATUSES))}."
            )
        proposals.append(
            RecruitmentProposal(
                role_id=str(item.get("role_id", "")),
                status=status,
                rationale=str(item.get("rationale", "")),
                role_labels=list(item.get("role_labels", [])),
                candidate=candidate,
                existing_employee=existing_employee,
                existing_employee_ids=list(item.get("existing_employee_ids", [])),
                metadata=dict(item.get("metadata", {})),
            )
        )
    return ensure_recruitment_plan_default_agents(
        RecruitmentPlan(
            company_profile=str(data.get("company_profile", "corporate")),
            proposals=proposals,
            recruiter_feedback=list(data.get("recruiter_feedback", [])),
            summary=str(data.get("summary", "")),
            metadata=dict(data.get("metadata", {})),
        )
    )


def build_recruitment_plan_from_payload(data: dict[str, Any]) -> RecruitmentPlan:
    return deserialize_recruitment_plan(data)


def build_recruitment_feedback(reply: str) -> str:
    cleaned = re.sub(r"\s+", " ", reply.strip())
    return cleaned


class CompanyRecruiter:
    """生成公司执行前的岗位人员配置方案。"""

    def __init__(self, llm: Any, org_engine: Any, talent_market: Any) -> None:
        self.llm = llm
        self.org_engine = org_engine
        self.talent_market = talent_market

    async def _llm_chat(self, llm: Any, system: str, payload: dict[str, Any]) -> str:
        from bandi.llm.accounted import accounted_complete

        response = await accounted_complete(
            llm,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            label="recruiter",
            session_id="recruiter",
        )
        return response.content if hasattr(response, "content") else str(response)

    def _build_organization_payload(self) -> dict[str, Any]:
        agents = list(self.org_engine.list_agents()) if self.org_engine else []
        role_ids = {
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in agents
            if str(getattr(agent, "role_id", "") or "").strip()
        }
        direct_reports: dict[str, list[str]] = {}
        for agent in agents:
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            manager_id = str(getattr(agent, "reports_to", "") or "").strip()
            if not role_id or manager_id == ROOT_MANAGER_ROLE_ID or manager_id not in role_ids:
                continue
            direct_reports.setdefault(manager_id, [])
            if role_id not in direct_reports[manager_id]:
                direct_reports[manager_id].append(role_id)

        org_graph: dict[str, list[str]] = {}
        for manager in agents:
            manager_id = str(getattr(manager, "role_id", "") or "").strip()
            children = list(direct_reports.get(manager_id) or [])
            if not manager_id or not children:
                continue
            spawn_order = [
                str(role_id or "").strip()
                for role_id in list(getattr(manager, "can_spawn", []) or [])
                if str(role_id or "").strip() in children
            ]
            standing_reports = [role_id for role_id in children if role_id not in spawn_order]
            org_graph[manager_id] = [*standing_reports, *spawn_order]

        final_decider_role_id = ""
        getter = getattr(self.org_engine, "get_final_decider_role_id", None)
        if callable(getter):
            try:
                final_decider_role_id = str(getter() or "").strip()
            except (RuntimeError, ValueError, AttributeError):
                logger.debug("failed to resolve final decider role for recruiter payload")

        return {
            "final_decider_role_id": final_decider_role_id,
            "org_graph": org_graph,
        }

    async def build_recruitment_plan(
        self,
        runtime_spec: Any,
        *,
        domains: list[str],
        project_id: str,
        recruiter_feedback: list[str] | None = None,
        recruitment_llm: Any | None = None,
        recruitment_agent: str | None = None,
    ) -> RecruitmentPlan:
        domains = list(domains or [])
        feedback = list(recruiter_feedback or [])
        active_llm = recruitment_llm if recruitment_llm is not None else self.llm
        selected_recruitment_agent = (
            normalize_recruitment_agent_choice(
                recruitment_agent,
                default="native",
            )
            or "native"
        )
        needs = self._collect_needs(runtime_spec)
        triage_by_role = await self._triage_staffing_for_needs(
            needs,
            recruiter_feedback=feedback,
            llm=active_llm,
        )
        prepared_needs: list[dict[str, Any]] = []
        selected_category_union: list[str] = []
        for need in needs:
            existing = self.org_engine.list_employees(role_id=need.role_id)
            triage_action, selected_categories, category_rationale = triage_by_role.get(
                need.role_id,
                ("direct_role_execution", [], "No staffing triage was produced for this role."),
            )
            for category in selected_categories:
                if category not in selected_category_union:
                    selected_category_union.append(category)
            prepared_needs.append(
                {
                    "need": need,
                    "existing_employees": list(existing),
                    "candidates": [],
                    "triage_action": triage_action,
                    "selected_categories": list(selected_categories),
                    "category_rationale": category_rationale,
                }
            )
        candidate_pool = self._recall_candidates_for_need(
            selected_categories=selected_category_union,
        )
        employee_pool = self._recall_existing_employee_pool(
            prepared_needs,
            candidate_pool=candidate_pool,
            selected_categories=selected_category_union,
        )
        for item in prepared_needs:
            item["candidates"] = candidate_pool
            item["employee_pool"] = employee_pool
        if active_llm:
            proposals = await self._recruit_globally(
                prepared_needs,
                recruiter_feedback=feedback,
                project_id=project_id,
                llm=active_llm,
                candidate_pool=candidate_pool,
                employee_pool=employee_pool,
            )
        else:
            proposals = [
                self._heuristic_proposal_for_prepared_need(item, project_id=project_id)
                for item in prepared_needs
            ]
        metadata = dict(getattr(runtime_spec, "metadata", {}) or {})
        plan_metadata = {
            "project_id": project_id,
            "execution_mode": str(metadata.get("execution_mode", "company_mode") or "company_mode"),
            "request_label": str(metadata.get("request_label", "runtime") or "runtime"),
            "recruitment_agent": selected_recruitment_agent,
        }
        # 副作用调用：填充 proposals 的 selected_execution_agent（就地修改）
        # 下方最终 plan 复用已填充的 proposals。勿删——看似脚手架实为必需步骤。
        plan = ensure_recruitment_plan_default_agents(
            RecruitmentPlan(
                company_profile=str(getattr(runtime_spec, "profile", "corporate") or "corporate"),
                proposals=proposals,
                recruiter_feedback=feedback,
                metadata=plan_metadata,
            )
        )
        summary = self.render_recruitment_summary(plan)
        return ensure_recruitment_plan_default_agents(
            RecruitmentPlan(
                company_profile=str(getattr(runtime_spec, "profile", "corporate") or "corporate"),
                proposals=proposals,
                recruiter_feedback=feedback,
                summary=summary,
                metadata=plan_metadata,
            )
        )

    def render_recruitment_summary(self, plan: RecruitmentPlan) -> str:
        execution_mode = str(plan.metadata.get("execution_mode", "company_mode") or "company_mode")
        if execution_mode == "task_mode":
            intro = "任务模式在执行前有待定的人员配置决策。"
            cancel_line = "回复 `deny` / `stop` 以取消任务模式执行。"
        else:
            intro = "公司模式在执行前有待定的人员配置决策。"
            cancel_line = "回复 `deny` / `stop` 以取消公司模式执行。"
        lines = [
            intro,
            "",
            f"Company profile: `{plan.company_profile}`",
        ]
        if plan.recruiter_feedback:
            lines.extend(
                [
                    "",
                    "Recruiter feedback so far:",
                    *[f"- {item}" for item in plan.recruiter_feedback[-_FEEDBACK_MAX_ITEMS:]],
                ]
            )
        if not plan.proposals:
            lines.extend(
                [
                    "",
                    "No staffing action is needed.",
                    "Reply `approve` to continue execution.",
                ]
            )
            return "\n".join(lines)

        lines.append("")
        lines.append("招聘决策（Recruitment decisions）：")
        for proposal in plan.proposals:
            role_label = proposal.role_labels[0] if proposal.role_labels else proposal.role_id
            if proposal.status == "direct_role_execution":
                lines.append(
                    f"- Role `{proposal.role_id}` ({role_label})：使用常规角色执行，无需人员配置。"
                )
                lines.append(f"  原因：{proposal.rationale}")
            elif proposal.status == "existing_staff":
                existing = ", ".join(proposal.existing_employee_ids) or "(现有员工)"
                selected = proposal.existing_employee
                if selected:
                    lines.append(
                        f"- Role `{proposal.role_id}` ({role_label})：保留现有员工 "
                        f"`{selected.employee_name}` ({selected.employee_id})。"
                    )
                    lines.append(
                        f"  经验分：{selected.experience_score}；已学技能：{len(selected.learned_skill_refs)}；"
                        f"现有池：{existing}"
                    )
                else:
                    lines.append(
                        f"- Role `{proposal.role_id}` ({role_label})：复用现有员工 {existing}。"
                    )
                lines.append(f"  原因：{proposal.rationale}")
            elif proposal.status == "proposed_hire" and proposal.candidate:
                lines.append(
                    f"- Role `{proposal.role_id}` ({role_label})：录用 `{proposal.candidate.template_name}` "
                    f"（员工 `{proposal.candidate.proposed_employee_name or proposal.candidate.proposed_employee_id}`）。"
                )
                if proposal.existing_employee_ids:
                    lines.append(f"  对比现有员工：{', '.join(proposal.existing_employee_ids)}")
                lines.append(f"  原因：{proposal.rationale or proposal.candidate.rationale}")
            else:
                lines.append(f"- Role `{proposal.role_id}` ({role_label})：回退为纯角色执行。")
                lines.append(f"  原因：{proposal.rationale}")
        lines.extend(
            [
                "",
                "回复 `approve` / `continue` 以接受以上录用并开始执行。",
                "回复 `deny` / `stop` / `cancel` 以拒绝该配置方案并停止执行。",
                "其他输入将被视为对配置方案的反馈或建议。",
                cancel_line,
            ]
        )
        return "\n".join(lines)

    def _collect_needs(self, runtime_spec: Any) -> list[RecruitmentNeed]:
        metadata = dict(getattr(runtime_spec, "metadata", {}) or {})
        request_text = str(
            getattr(runtime_spec, "original_request", "")
            or metadata.get("original_request", "")
            or ""
        ).strip()
        grouped: dict[str, RecruitmentNeed] = {}
        for agent in self.org_engine.list_agents():
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            if not role_id or role_id == TASK_GENERALIST_ROLE_ID:
                continue
            grouped[role_id] = RecruitmentNeed(
                role_id=role_id,
                role_name=str(getattr(agent, "name", "") or role_id),
                role_responsibility=str(getattr(agent, "responsibility", "") or "").strip(),
                request_text=request_text,
            )
        for employee in self.org_engine.list_employees():
            role_id = str(getattr(employee, "role_id", "") or "").strip()
            if not role_id or role_id == TASK_GENERALIST_ROLE_ID or role_id in grouped:
                continue
            grouped[role_id] = RecruitmentNeed(
                role_id=role_id,
                role_name=role_id,
                role_responsibility="",
                request_text=request_text,
            )
        return list(grouped.values())

    def _recall_candidates_for_need(
        self,
        *,
        selected_categories: list[str],
    ) -> list[Any]:
        merged: dict[str, Any] = {}
        for candidate in self.talent_market.list_templates_by_categories(
            categories=selected_categories
        ):
            candidate_id = str(getattr(candidate, "id", "") or candidate.name or "").strip()
            if candidate_id and candidate_id not in merged:
                merged[candidate_id] = candidate
        return list(merged.values())

    def _recall_existing_employee_pool(
        self,
        prepared_needs: list[dict[str, Any]],
        *,
        candidate_pool: list[Any],
        selected_categories: list[str],
    ) -> list[Any]:
        categories = {
            str(category or "").strip().lower()
            for category in list(selected_categories or [])
            if str(category or "").strip()
        }
        candidate_template_ids = {
            str(getattr(candidate, "id", "") or "").strip()
            or str(getattr(candidate, "name", "") or "").strip()
            for candidate in list(candidate_pool or [])
            if str(getattr(candidate, "id", "") or getattr(candidate, "name", "") or "").strip()
        }
        same_role_ids = {
            str(getattr(employee, "employee_id", "") or "").strip()
            for item in prepared_needs
            for employee in list(item.get("existing_employees") or [])
            if str(getattr(employee, "employee_id", "") or "").strip()
        }
        employees = self.org_engine.list_employees()
        selected: dict[str, Any] = {}
        for employee in employees:
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if not employee_id:
                continue
            metadata = dict(getattr(employee, "metadata", {}) or {})
            if metadata.get("is_default_employee") or metadata.get("is_fallback_employee"):
                continue
            category = str(getattr(employee, "category", "") or "").strip().lower()
            template_id = str(getattr(employee, "template_id", "") or "").strip()
            employee_domains = {
                str(domain or "").strip().lower()
                for domain in list(getattr(employee, "domains", []) or [])
                if str(domain or "").strip()
            }
            if (
                employee_id in same_role_ids
                or category in categories
                or template_id in candidate_template_ids
                or bool(employee_domains & categories)
            ):
                selected[employee_id] = employee
        return list(selected.values())

    def _heuristic_proposal_for_prepared_need(
        self,
        item: dict[str, Any],
        *,
        project_id: str,
    ) -> RecruitmentProposal:
        need: RecruitmentNeed = item["need"]
        existing_employees = list(item.get("existing_employees") or [])
        candidates = list(item.get("candidates") or [])
        triage_action = str(item.get("triage_action", "") or "")
        selected_categories = list(item.get("selected_categories") or [])
        category_rationale = str(item.get("category_rationale", "") or "")
        metadata = {
            "triage_action": triage_action,
            "selected_categories": selected_categories,
            "category_rationale": category_rationale,
        }
        role_labels = [need.role_name] if need.role_name else []
        if triage_action == "direct_role_execution":
            return RecruitmentProposal(
                role_id=need.role_id,
                status="direct_role_execution",
                rationale=category_rationale or "常规角色执行已足够，无需人员配置对比。",
                role_labels=role_labels,
                existing_employee_ids=[employee.employee_id for employee in existing_employees],
                metadata={**metadata, "selection_source": "heuristic_direct"},
            )
        if existing_employees:
            existing_payload = [
                self._build_existing_employee_summary(
                    employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                )
                for employee in existing_employees
            ]
            selected = max(
                existing_payload, key=lambda item: float(item.get("experience_score", 0.0))
            )
            employee = next(
                emp for emp in existing_employees if emp.employee_id == selected["employee_id"]
            )
            recommendation = self._make_existing_employee_recommendation(
                employee,
                rationale="启发式选择：存在有经验的现有员工可复用。",
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="existing_staff",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                existing_employee=recommendation,
                existing_employee_ids=[employee.employee_id for employee in existing_employees],
                metadata={**metadata, "selection_source": "heuristic_existing"},
            )
        if candidates:
            candidate = candidates[0]
            recommendation = self._make_candidate_recommendation(
                candidate,
                rationale="启发式选择：无招募 LLM 可用，取首个匹配模板。",
                role_id=need.role_id,
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                candidate=recommendation,
                metadata={**metadata, "selection_source": "heuristic_hire"},
            )
        return RecruitmentProposal(
            role_id=need.role_id,
            status="fallback_role_only",
            rationale="没有可信的人才模板匹配该岗位需求，回退为纯角色执行。",
            role_labels=role_labels,
            metadata={
                **metadata,
                "selection_source": "heuristic_fallback",
                "fallback_reason": "no_candidate_templates",
            },
        )

    async def _recruit_globally(
        self,
        prepared_needs: list[dict[str, Any]],
        *,
        recruiter_feedback: list[str],
        project_id: str,
        llm: Any,
        candidate_pool: list[Any],
        employee_pool: list[Any],
    ) -> list[RecruitmentProposal]:
        if not prepared_needs:
            return []
        role_payloads: list[dict[str, Any]] = []
        prepared_by_role: dict[str, dict[str, Any]] = {}
        role_order: list[str] = []
        candidate_payload = [
            self.talent_market.build_candidate_summary(candidate) for candidate in candidate_pool
        ]
        employee_pool_payload = [
            self._build_existing_employee_summary(
                employee,
                role_id=str(getattr(employee, "role_id", "") or ""),
                domains=[],
                project_id=project_id,
            )
            for employee in employee_pool
        ]
        for item in prepared_needs:
            need: RecruitmentNeed = item["need"]
            existing_employees = list(item.get("existing_employees") or [])
            existing_payload = [
                self._build_existing_employee_summary(
                    employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                )
                for employee in existing_employees
            ]
            prepared_item = {
                **item,
                "existing_payload": existing_payload,
            }
            prepared_by_role[need.role_id] = prepared_item
            role_order.append(need.role_id)
            role_payloads.append(
                {
                    "role_id": need.role_id,
                    "role_name": need.role_name,
                    "role_responsibility": need.role_responsibility,
                    "user_request": need.request_text,
                    "triage_action": str(item.get("triage_action", "") or ""),
                    "selected_categories": list(item.get("selected_categories") or []),
                    "category_rationale": str(item.get("category_rationale", "") or ""),
                    "existing_employees": existing_payload,
                    "current_role_employee_ids": [
                        str(employee.get("employee_id", "") or "").strip()
                        for employee in existing_payload
                        if str(employee.get("employee_id", "") or "").strip()
                    ],
                }
            )
        prompt_payload = {
            "project_id": project_id,
            "recruiter_feedback": list(recruiter_feedback),
            **self._build_organization_payload(),
            "employee_pool": employee_pool_payload,
            "candidate_pool": candidate_payload,
            "roles": role_payloads,
        }
        return await self._llm_retry_loop(
            llm=llm,
            prompt=GLOBAL_RECRUITER_PROMPT,
            base_payload=prompt_payload,
            array_key="proposals",
            failure_label="Global recruiter",
            build=lambda items, attempt: self._build_global_proposals_from_response(
                items,
                prepared_by_role=prepared_by_role,
                role_order=role_order,
                project_id=project_id,
                attempt=attempt,
                candidate_pool=candidate_pool,
                employee_pool=employee_pool,
            ),
            fallback=lambda: [
                self._heuristic_proposal_for_prepared_need(item, project_id=project_id)
                for item in prepared_needs
            ],
        )

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any]:
        return parse_llm_json_strict(raw)

    async def _llm_retry_loop(
        self,
        *,
        llm: Any,
        prompt: str,
        base_payload: dict[str, Any],
        array_key: str,
        failure_label: str,
        build: Callable[[list[Any], int], _T],
        fallback: Callable[[], _T],
    ) -> _T:
        """LLM 重试循环共享骨架（DRY）：payload 拷贝 → JSON 解析 → 数组校验 → 构建/兜底。"""
        retry_feedback: list[str] = []
        for attempt in range(1, _RECRUITER_MAX_RETRIES + 1):
            payload = dict(base_payload)
            if retry_feedback:
                payload["retry_feedback"] = list(retry_feedback)
            try:
                raw = await self._llm_chat(llm, prompt, payload)
            except (RuntimeError, ValueError, AttributeError) as exc:
                retry_feedback.append(
                    f"{failure_label} LLM call failed: {type(exc).__name__}: {exc}"
                )
                continue
            try:
                data = self._parse_llm_json(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                retry_feedback.append(f"Response was not valid JSON: {exc}")
                continue
            items = data.get(array_key)
            if not isinstance(items, list):
                retry_feedback.append(f"Response must contain a `{array_key}` array.")
                continue
            try:
                return build(items, attempt)
            except ValueError as exc:
                retry_feedback.append(str(exc))
        logger.warning(
            f"{failure_label} exhausted retries; falling back to heuristic staffing decisions"
        )
        return fallback()

    def _build_global_proposals_from_response(
        self,
        proposals_data: list[Any],
        *,
        prepared_by_role: dict[str, dict[str, Any]],
        role_order: list[str],
        project_id: str,
        attempt: int,
        candidate_pool: list[Any],
        employee_pool: list[Any],
    ) -> list[RecruitmentProposal]:
        expected_roles = set(role_order)
        proposal_by_role: dict[str, RecruitmentProposal] = {}
        candidate_by_id = {
            str(getattr(candidate, "id", "") or "").strip()
            or str(getattr(candidate, "name", "") or "").strip(): candidate
            for candidate in candidate_pool
            if str(getattr(candidate, "id", "") or getattr(candidate, "name", "") or "").strip()
        }
        global_employee_by_id = {
            str(getattr(employee, "employee_id", "") or "").strip(): employee
            for employee in employee_pool
            if str(getattr(employee, "employee_id", "") or "").strip()
        }
        for index, raw_item in enumerate(proposals_data):
            if not isinstance(raw_item, dict):
                raise ValueError(f"Proposal at index {index} must be a JSON object.")
            role_id = str(raw_item.get("role_id", "") or "").strip()
            if role_id not in expected_roles:
                raise ValueError(
                    f"Invalid role_id `{role_id}`. Choose one of: {', '.join(sorted(expected_roles))}."
                )
            if role_id in proposal_by_role:
                raise ValueError(f"Duplicate proposal for role_id `{role_id}`.")
            status = str(raw_item.get("status", "fallback_role_only") or "").strip().lower()
            if status not in VALID_RECRUITMENT_STATUSES:
                raise ValueError(
                    f"Invalid status `{status}` for role `{role_id}`. "
                    f"Choose one of: {', '.join(sorted(VALID_RECRUITMENT_STATUSES))}."
                )
            item = prepared_by_role[role_id]
            need: RecruitmentNeed = item["need"]
            existing_employees = list(item.get("existing_employees") or [])
            employee_by_id = {
                str(getattr(employee, "employee_id", "") or "").strip(): employee
                for employee in existing_employees
                if str(getattr(employee, "employee_id", "") or "").strip()
            }
            metadata = {
                "selection_source": "llm_global",
                "attempts": attempt,
                "triage_action": str(item.get("triage_action", "") or ""),
                "selected_categories": list(item.get("selected_categories") or []),
                "category_rationale": str(item.get("category_rationale", "") or ""),
            }
            rationale = str(raw_item.get("rationale", "") or "").strip()
            role_labels = [need.role_name] if need.role_name else []
            existing_employee_ids = list(employee_by_id)
            if status == "direct_role_execution":
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="direct_role_execution",
                    rationale=rationale
                    or "Recruiter determined that ordinary role execution is sufficient.",
                    role_labels=role_labels,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            if status == "fallback_role_only":
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="fallback_role_only",
                    rationale=rationale
                    or "Recruiter determined that no provided option was a credible fit.",
                    role_labels=role_labels,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            if status == "existing_staff":
                employee_id = str(raw_item.get("employee_id", "") or "").strip()
                if employee_id not in global_employee_by_id:
                    raise ValueError(
                        f"Invalid employee_id `{employee_id}` for role `{role_id}`. "
                        f"Choose one of: {', '.join(sorted(global_employee_by_id))}."
                    )
                recommendation = self._make_existing_employee_recommendation(
                    global_employee_by_id[employee_id],
                    rationale=rationale
                    or "Recruiter selected the existing employee based on role fit and prior experience.",
                )
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="existing_staff",
                    rationale=recommendation.rationale,
                    role_labels=role_labels,
                    existing_employee=recommendation,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            template_id = str(raw_item.get("template_id", "") or "").strip()
            if template_id not in candidate_by_id:
                raise ValueError(
                    f"Invalid template_id `{template_id}` for role `{role_id}`. "
                    f"Choose one of: {', '.join(sorted(candidate_by_id))}."
                )
            recommendation = self._make_candidate_recommendation(
                candidate_by_id[template_id],
                rationale=rationale
                or "Recruiter selected this candidate based on role and domain fit.",
                role_id=role_id,
                proposed_employee_name=str(
                    raw_item.get("proposed_employee_name", "") or ""
                ).strip(),
            )
            proposal_by_role[role_id] = RecruitmentProposal(
                role_id=role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                candidate=recommendation,
                existing_employee_ids=existing_employee_ids,
                metadata=metadata,
            )
        missing = [role_id for role_id in role_order if role_id not in proposal_by_role]
        if missing:
            raise ValueError(f"Missing proposals for role_id(s): {', '.join(missing)}.")
        return [proposal_by_role[role_id] for role_id in role_order]

    async def _triage_staffing_for_needs(
        self,
        needs: list[RecruitmentNeed],
        *,
        recruiter_feedback: list[str],
        llm: Any | None = None,
    ) -> dict[str, tuple[str, list[str], str]]:
        if not needs:
            return {}
        category_catalog = self.talent_market.list_category_catalog()
        valid_categories = {
            str(item.get("category", "") or "").strip().lower()
            for item in category_catalog
            if str(item.get("category", "") or "").strip()
        }
        active_llm = llm if llm is not None else self.llm
        if not category_catalog or not active_llm:
            return {
                need.role_id: self._heuristic_staffing_triage(need, category_catalog)
                for need in needs
            }

        payload = {
            "user_request": next((need.request_text for need in needs if need.request_text), ""),
            "recruiter_feedback": recruiter_feedback,
            **self._build_organization_payload(),
            "roles": [
                {
                    "role_id": need.role_id,
                    "role_name": need.role_name,
                    "role_responsibility": need.role_responsibility,
                }
                for need in needs
            ],
            "category_catalog": category_catalog,
        }
        expected_roles = {need.role_id for need in needs}
        return await self._llm_retry_loop(
            llm=active_llm,
            prompt=STAFFING_TRIAGE_PROMPT,
            base_payload=payload,
            array_key="roles",
            failure_label="Staffing triage",
            build=lambda items, _attempt: self._build_triage_plan_from_response(
                items,
                expected_roles=expected_roles,
                valid_categories=valid_categories,
            ),
            fallback=lambda: {
                need.role_id: self._heuristic_staffing_triage(need, category_catalog)
                for need in needs
            },
        )

    @staticmethod
    def _build_triage_plan_from_response(
        roles_data: list[Any],
        *,
        expected_roles: set[str],
        valid_categories: set[str],
    ) -> dict[str, tuple[str, list[str], str]]:
        triage_by_role: dict[str, tuple[str, list[str], str]] = {}
        for index, raw_item in enumerate(roles_data):
            if not isinstance(raw_item, dict):
                raise ValueError(f"Triage entry at index {index} must be a JSON object.")
            role_id = str(raw_item.get("role_id", "") or "").strip()
            if role_id not in expected_roles:
                raise ValueError(
                    f"Invalid role_id `{role_id}`. Choose one of: {', '.join(sorted(expected_roles))}."
                )
            if role_id in triage_by_role:
                raise ValueError(f"Duplicate triage entry for role_id `{role_id}`.")
            action = str(raw_item.get("action", "category_screening") or "").strip().lower()
            rationale = str(raw_item.get("rationale", "") or "").strip()
            if action == "direct_role_execution":
                triage_by_role[role_id] = (action, [], rationale)
                continue
            if action != "category_screening":
                raise ValueError(
                    f"Invalid action `{action}` for role `{role_id}`. "
                    "Choose `direct_role_execution` or `category_screening`."
                )
            chosen = [
                str(category).strip().lower()
                for category in raw_item.get("categories", [])
                if str(category).strip()
            ]
            chosen = [category for category in chosen if category in valid_categories]
            chosen = list(dict.fromkeys(chosen))[:_MAX_SELECTED_CATEGORIES]
            if not chosen:
                raise ValueError(
                    f"Role `{role_id}` chose category_screening without valid categories. "
                    f"Choose 1 to 3 valid categories from: {', '.join(sorted(valid_categories))}."
                )
            triage_by_role[role_id] = ("category_screening", chosen, rationale)
        missing = [role_id for role_id in sorted(expected_roles) if role_id not in triage_by_role]
        if missing:
            raise ValueError(f"Missing triage entries for role_id(s): {', '.join(missing)}.")
        return triage_by_role

    @staticmethod
    def _need_text(need: RecruitmentNeed) -> str:
        return " ".join(
            text for text in (need.role_name, need.role_responsibility, need.request_text) if text
        ).strip()

    def _heuristic_staffing_triage(
        self,
        need: RecruitmentNeed,
        category_catalog: list[dict[str, Any]],
    ) -> tuple[str, list[str], str]:
        combined_text = self._need_text(need).lower()
        if len(combined_text) <= _TRIVIAL_REQUEST_MAX_LEN or combined_text in {
            "hi",
            "hello",
            "你好",
            "您好",
            "thanks",
            "谢谢",
        }:
            return "direct_role_execution", [], "请求足够简单，无需人员配置。"
        if not category_catalog:
            return "direct_role_execution", [], "没有可用的人才分类目录，使用常规角色执行。"
        selected = self._heuristic_category_selection(need, category_catalog)
        if not selected:
            return "direct_role_execution", [], "没有明显的人才分类能匹配。"
        return "category_screening", selected, "根据分类描述启发式选择。"

    def _heuristic_category_selection(
        self,
        need: RecruitmentNeed,
        category_catalog: list[dict[str, Any]],
    ) -> list[str]:
        need_tokens = set(
            re.findall(
                r"[a-z0-9][a-z0-9+-]{2,}",
                self._need_text(need).lower(),
            )
        )
        scored: list[tuple[float, str]] = []
        for item in category_catalog:
            category = str(item.get("category", "")).strip().lower()
            description = str(item.get("description", "")).strip().lower()
            template_count = int(item.get("template_count", 0) or 0)
            category_tokens = set(
                re.findall(r"[a-z0-9][a-z0-9+-]{2,}", f"{category} {description}")
            )
            score = (
                float(len(need_tokens & category_tokens))
                + min(template_count, _CATEGORY_TEMPLATE_BONUS_CAP)
                * _CATEGORY_TEMPLATE_BONUS_WEIGHT
            )
            scored.append((score, category))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = [category for score, category in scored if score > 0][:_MAX_SELECTED_CATEGORIES]
        if selected:
            return selected
        return [item["category"] for item in category_catalog[:_MAX_FALLBACK_CATEGORIES]]

    @staticmethod
    def _metadata_experience_score(metadata: dict[str, object]) -> float:
        numeric_keys = (
            "experience_score",
            "years_experience",
            "experience_years",
            "completed_work_items",
            "tasks_completed",
            "completed_count",
        )
        for key in numeric_keys:
            try:
                score = float(metadata.get(key, 0.0))
            except (TypeError, ValueError):
                continue
            if score > 0.0:
                return score
        return 0.0

    def _build_existing_employee_summary(
        self,
        employee: Any,
        *,
        role_id: str = "",
        domains: list[str] = (),
        project_id: str = "",
    ) -> dict[str, Any]:
        # domains/project_id 为预留参数（未来按领域/项目过滤员工池）
        # 当前实现不消费——保留以稳定调用契约（保守处理）。
        del role_id, domains, project_id
        metadata = dict(getattr(employee, "metadata", {}) or {})
        employee_id = str(getattr(employee, "employee_id", "") or "").strip()
        employee_name = str(getattr(employee, "name", "") or "").strip()
        template_id = str(getattr(employee, "template_id", "") or "").strip()
        home_role_id = str(getattr(employee, "role_id", "") or "").strip()
        category = str(getattr(employee, "category", "") or "").strip()
        description = str(getattr(employee, "description", "") or "").strip()
        return {
            "employee_id": employee_id,
            "employee_name": employee_name,
            "template_id": template_id,
            "home_role_id": home_role_id,
            "role_ids": (
                list(self.org_engine.employee_role_ids(employee))
                if hasattr(self.org_engine, "employee_role_ids")
                else [home_role_id]
            ),
            "category": category,
            "experience_score": self._metadata_experience_score(metadata),
            "learned_skill_refs": list(
                metadata.get("learned_skill_refs") or metadata.get("skills") or []
            ),
            "description": description,
        }

    def _make_existing_employee_recommendation(
        self,
        employee: Any,
        *,
        rationale: str,
    ) -> RecruitmentEmployeeRecommendation:
        summary = self._build_existing_employee_summary(employee)
        return RecruitmentEmployeeRecommendation(
            employee_id=employee.employee_id,
            employee_name=employee.name,
            role_id=employee.role_id,
            category=employee.category,
            domains=list(employee.domains),
            learned_skill_refs=list(summary.get("learned_skill_refs", [])),
            experience_score=float(summary.get("experience_score", 0.0) or 0.0),
            rationale=rationale,
            metadata={"template_id": employee.template_id, "experience_mode": "with_experience"},
        )

    def _make_candidate_recommendation(
        self,
        candidate: Any,
        *,
        rationale: str,
        role_id: str,
        proposed_employee_name: str = "",
    ) -> RecruitmentCandidateRecommendation:
        candidate_name = str(getattr(candidate, "name", "") or "").strip()
        candidate_id = str(getattr(candidate, "id", "") or "").strip() or candidate_name
        employee_name = str(proposed_employee_name or "").strip() or candidate_name or candidate_id
        employee_id = self.talent_market.build_employee_id(
            role_id=role_id, template_id=candidate_id
        )
        return RecruitmentCandidateRecommendation(
            template_id=candidate_id,
            template_name=candidate_name,
            category=getattr(candidate, "category", ""),
            domains=list(getattr(candidate, "domains", []) or []),
            prompt_ref=getattr(candidate, "prompt_ref", ""),
            preferred_external_agent=getattr(candidate, "preferred_external_agent", None),
            source_path=str(getattr(candidate, "source_path", "") or "").strip(),
            rationale=rationale,
            proposed_employee_name=employee_name,
            proposed_employee_id=employee_id,
            metadata={
                "source_repo": str(getattr(candidate, "source_repo", "") or "").strip(),
                "experience_mode": "template_only",
            },
        )
