from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.recruiter import (
    CompanyRecruiter,
    Recruiter,
    RecruitmentCandidateRecommendation,
    RecruitmentEmployeeRecommendation,
    RecruitmentNeed,
    RecruitmentPlan,
    RecruitmentProposal,
    RecruitStatus,
    apply_recruitment_role_agent_overrides,
    build_fallback_role_ids,
    build_recruitment_feedback,
    build_recruitment_plan_from_payload,
    build_staffing_experience_modes,
    build_staffing_overrides,
    deserialize_recruitment_plan,
    ensure_recruitment_plan_default_agents,
    extract_recruitment_role_agent_overrides,
    normalize_recruitment_agent_choice,
    recruitment_plan_requires_confirmation,
    resolve_effective_execution_agent,
    serialize_recruitment_plan,
)
from bandi.company.talent_market import TalentMarket


class TestRecruiter:
    def test_register_and_recruit(self):
        r = Recruiter()
        r.register_seat("worker")
        seat = r.recruit("worker")
        assert seat is not None
        assert seat.status == RecruitStatus.RECRUITED

    def test_recruit_unregistered(self):
        assert Recruiter().recruit("nonexistent") is None

    def test_recruit_already_busy(self):
        r = Recruiter()
        r.register_seat("worker")
        r.recruit("worker")
        assert r.recruit("worker") is None

    def test_release(self):
        r = Recruiter()
        r.register_seat("worker")
        r.recruit("worker")
        assert r.release("worker")
        assert r.get_seat("worker").status == RecruitStatus.AVAILABLE

    def test_release_allows_recruit_again(self):
        r = Recruiter()
        r.register_seat("worker")
        r.recruit("worker")
        r.release("worker")
        seat = r.recruit("worker")
        assert seat is not None
        assert seat.status == RecruitStatus.RECRUITED

    def test_list_available(self):
        r = Recruiter()
        r.register_seat("a")
        r.register_seat("b")
        r.recruit("a")
        assert len(r.list_available()) == 1
        assert r.list_available()[0].role_id == "b"

    def test_dispatch_success(self):
        r = Recruiter()
        r.register_seat("worker")
        assert r.dispatch("worker")
        assert r.get_seat("worker").status == RecruitStatus.BUSY

    def test_dispatch_fail(self):
        assert not Recruiter().dispatch("nonexistent")


class TestRecruitmentModels:
    def _candidate(self) -> RecruitmentCandidateRecommendation:
        return RecruitmentCandidateRecommendation(
            template_id="general_engineer",
            template_name="通用软件工程师",
            category="engineering",
            domains=["python", "sql"],
            prompt_ref="talent/engineer.md",
            preferred_external_agent="codex",
            source_path="templates/engineer.md",
            rationale="fits role",
            proposed_employee_name="工程一号",
            proposed_employee_id="general_engineer",
            metadata={"source_repo": "openopc", "experience_mode": "template_only"},
        )

    def _employee(self) -> RecruitmentEmployeeRecommendation:
        return RecruitmentEmployeeRecommendation(
            employee_id="e1",
            employee_name="Alice",
            role_id="engineer",
            category="engineering",
            domains=["python"],
            learned_skill_refs=["coding"],
            experience_score=0.8,
            rationale="has experience",
            metadata={"template_id": "general_engineer", "experience_mode": "with_experience"},
        )

    def _proposal(self) -> RecruitmentProposal:
        return RecruitmentProposal(
            role_id="engineer",
            status="proposed_hire",
            rationale="fits role",
            role_labels=["Engineer"],
            candidate=self._candidate(),
            existing_employee_ids=["e1"],
            metadata={"selection_source": "llm_global", "attempts": 1},
        )

    def test_serialize_deserialize_roundtrip(self):
        plan = RecruitmentPlan(
            company_profile="corporate",
            proposals=[self._proposal()],
            recruiter_feedback=["prefer experienced"],
            summary="Hire one engineer.",
            metadata={"project_id": "p1", "execution_mode": "company_mode"},
        )
        restored = deserialize_recruitment_plan(serialize_recruitment_plan(plan))
        assert restored.company_profile == "corporate"
        assert restored.metadata["project_id"] == "p1"
        assert restored.recruiter_feedback == ["prefer experienced"]
        proposal = restored.proposals[0]
        assert proposal.role_id == "engineer"
        assert proposal.status == "proposed_hire"
        assert proposal.candidate is not None
        assert proposal.candidate.template_id == "general_engineer"
        assert proposal.candidate.domains == ["python", "sql"]
        assert proposal.candidate.proposed_employee_id == "general_engineer"
        assert proposal.candidate.metadata["experience_mode"] == "template_only"
        assert proposal.existing_employee_ids == ["e1"]

    def test_serialize_employee_proposal_roundtrip(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(
                    role_id="engineer",
                    status="existing_staff",
                    rationale="keep",
                    role_labels=["Engineer"],
                    existing_employee=self._employee(),
                    existing_employee_ids=["e1"],
                )
            ]
        )
        restored = deserialize_recruitment_plan(serialize_recruitment_plan(plan))
        employee = restored.proposals[0].existing_employee
        assert employee is not None
        assert employee.employee_id == "e1"
        assert employee.experience_score == 0.8
        assert employee.learned_skill_refs == ["coding"]

    def test_deserialize_empty(self):
        plan = deserialize_recruitment_plan({})
        assert plan.proposals == []
        assert plan.company_profile == "corporate"
        assert plan.metadata == {}

    def test_deserialize_status_normalized(self):
        plan = deserialize_recruitment_plan(
            {
                "proposals": [
                    {
                        "role_id": "engineer",
                        "status": " EXISTING_STAFF ",
                        "rationale": "r",
                    }
                ]
            }
        )
        assert plan.proposals[0].status == "existing_staff"

    def test_deserialize_invalid_status_raises(self):
        with pytest.raises(ValueError):
            deserialize_recruitment_plan(
                {"proposals": [{"role_id": "engineer", "status": "bogus", "rationale": "r"}]}
            )


class TestRecruiterPureFunctions:
    def test_normalize_agent_choice(self):
        assert normalize_recruitment_agent_choice("codex") == "codex"
        assert normalize_recruitment_agent_choice("claude-code") == "claude_code"
        assert normalize_recruitment_agent_choice("opencode") == "opencode"
        assert normalize_recruitment_agent_choice("bogus") is None
        assert normalize_recruitment_agent_choice("", default="cursor") == "cursor"
        assert normalize_recruitment_agent_choice("", default="bogus") is None

    def test_resolve_effective_execution_agent(self):
        display, assigned, force_native = resolve_effective_execution_agent("native")
        assert (display, assigned, force_native) == ("native", None, True)
        display, assigned, force_native = resolve_effective_execution_agent("codex")
        assert (display, assigned, force_native) == ("codex", "codex", False)
        display, assigned, force_native = resolve_effective_execution_agent(
            None, preferred_external_agent="claude_code"
        )
        assert (display, assigned, force_native) == ("claude_code", "claude_code", False)
        display, assigned, force_native = resolve_effective_execution_agent(
            "codex", force_native_execution=True
        )
        assert (display, assigned, force_native) == ("native", None, True)

    def test_ensure_plan_default_agents(self):
        plan = ensure_recruitment_plan_default_agents(
            RecruitmentPlan(
                proposals=[RecruitmentProposal(role_id="engineer", status="proposed_hire")]
            )
        )
        assert plan.proposals[0].metadata["selected_execution_agent"] == "codex"

    def test_apply_role_agent_overrides(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(role_id="engineer", status="proposed_hire"),
                RecruitmentProposal(role_id="analyst", status="proposed_hire"),
            ]
        )
        apply_recruitment_role_agent_overrides(plan, {"engineer": "cursor", "analyst": "bogus"})
        assert plan.proposals[0].metadata["selected_execution_agent"] == "cursor"
        assert "selected_execution_agent" not in plan.proposals[1].metadata

    def test_extract_role_agent_overrides(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(
                    role_id="engineer",
                    status="proposed_hire",
                    metadata={"selected_execution_agent": "codex"},
                )
            ]
        )
        assert extract_recruitment_role_agent_overrides(plan) == {"engineer": "codex"}

    def test_build_staffing_overrides(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(
                    role_id="engineer",
                    status="existing_staff",
                    existing_employee=RecruitmentEmployeeRecommendation(
                        employee_id="e1", employee_name="Alice", role_id="engineer"
                    ),
                ),
                RecruitmentProposal(
                    role_id="analyst",
                    status="proposed_hire",
                    candidate=RecruitmentCandidateRecommendation(
                        template_id="data_analyst",
                        template_name="数据分析师",
                        proposed_employee_id="data_analyst",
                    ),
                ),
            ]
        )
        assert build_staffing_overrides(plan) == {"engineer": "e1", "analyst": "data_analyst"}

    def test_build_staffing_experience_modes(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(
                    role_id="engineer",
                    status="existing_staff",
                    existing_employee=RecruitmentEmployeeRecommendation(
                        employee_id="e1", employee_name="Alice", role_id="engineer"
                    ),
                ),
                RecruitmentProposal(
                    role_id="analyst",
                    status="proposed_hire",
                    candidate=RecruitmentCandidateRecommendation(
                        template_id="data_analyst", template_name="数据分析师"
                    ),
                ),
                RecruitmentProposal(role_id="writer", status="fallback_role_only"),
            ]
        )
        assert build_staffing_experience_modes(plan) == {
            "engineer": "with_experience",
            "analyst": "template_only",
        }

    def test_build_fallback_role_ids(self):
        plan = RecruitmentPlan(
            proposals=[
                RecruitmentProposal(role_id="writer", status="fallback_role_only"),
                RecruitmentProposal(role_id="engineer", status="proposed_hire"),
            ]
        )
        assert build_fallback_role_ids(plan) == {"writer"}

    def test_requires_confirmation(self):
        assert recruitment_plan_requires_confirmation(
            RecruitmentPlan(
                proposals=[RecruitmentProposal(role_id="engineer", status="proposed_hire")]
            )
        )
        assert not recruitment_plan_requires_confirmation(
            RecruitmentPlan(
                proposals=[RecruitmentProposal(role_id="writer", status="fallback_role_only")]
            )
        )

    def test_build_feedback_compacts_whitespace(self):
        assert (
            build_recruitment_feedback("  prefer   a senior \n engineer  ")
            == "prefer a senior engineer"
        )

    def test_build_plan_from_payload(self):
        plan = build_recruitment_plan_from_payload(
            {"company_profile": "corporate", "proposals": []}
        )
        assert plan.company_profile == "corporate"
        assert plan.proposals == []


class _MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.calls.append(messages)
        content = self._responses.pop(0) if self._responses else "{}"
        return SimpleNamespace(content=content)


class _FakeOrg:
    def __init__(self, agents: list[Any], employees: list[Any]) -> None:
        self._agents = agents
        self._employees = employees

    def list_agents(self):
        return list(self._agents)

    def list_employees(self, role_id: str | None = None):
        if role_id is None:
            return list(self._employees)
        return [e for e in self._employees if e.role_id == role_id]

    def get_final_decider_role_id(self) -> str:
        return "ceo"

    def employee_role_ids(self, employee):
        return [employee.role_id]


class _FakeSpec:
    profile = "corporate"
    original_request = "build a data dashboard with sql and reporting"
    metadata = {
        "execution_mode": "company_mode",
        "request_label": "runtime",
        "original_request": "build a data dashboard with sql and reporting",
    }


class TestCompanyRecruiter:
    def _recruiter(self, org=None, llm=None):
        return CompanyRecruiter(
            llm=llm,
            org_engine=org if org is not None else OrgEngine(),
            talent_market=TalentMarket(templates_dir="/nonexistent"),
        )

    def test_heuristic_triage_simple_request_direct(self):
        recruiter = self._recruiter()
        need = RecruitmentNeed(role_id="ceo", role_name="CEO", request_text="hi")
        catalog = recruiter.talent_market.list_category_catalog()
        action, categories, _ = recruiter._heuristic_staffing_triage(need, catalog)
        assert action == "direct_role_execution"
        assert categories == []

    @pytest.mark.asyncio
    async def test_build_plan_heuristic_hire_without_llm(self):
        recruiter = self._recruiter()
        plan = await recruiter.build_recruitment_plan(_FakeSpec(), domains=[], project_id="p1")
        assert plan.proposals
        for proposal in plan.proposals:
            assert proposal.status == "proposed_hire"
            assert proposal.candidate is not None
        assert "Recruitment decisions" in plan.summary or "招聘决策" in plan.summary

    @pytest.mark.asyncio
    async def test_build_plan_heuristic_existing_staff(self):
        org = OrgEngine()
        org.register_employee(
            OrgEmployee(
                employee_id="e1",
                name="Alice",
                role_id="engineer",
                category="engineering",
                domains=["python"],
            )
        )
        recruiter = self._recruiter(org=org)
        plan = await recruiter.build_recruitment_plan(_FakeSpec(), domains=[], project_id="p1")
        engineer = next(p for p in plan.proposals if p.role_id == "engineer")
        assert engineer.status == "existing_staff"
        assert engineer.existing_employee is not None
        assert engineer.existing_employee.employee_id == "e1"
        assert engineer.metadata["selection_source"] == "heuristic_existing"

    def test_heuristic_fallback_without_pool_or_candidates(self):
        recruiter = self._recruiter()
        item = {
            "need": RecruitmentNeed(role_id="writer", role_name="Writer"),
            "existing_employees": [],
            "candidates": [],
            "employee_pool": [],
            "triage_action": "category_screening",
            "selected_categories": ["content"],
            "category_rationale": "screened",
        }
        proposal = recruiter._heuristic_proposal_for_prepared_need(item, project_id="p1")
        assert proposal.status == "fallback_role_only"
        assert proposal.metadata["fallback_reason"] == "no_candidate_templates"

    def test_existing_employee_summary_getattr_defensive(self):
        recruiter = self._recruiter()

        class PartialEmployee:
            employee_id = "e1"
            role_id = "engineer"
            metadata = {}

        summary = recruiter._build_existing_employee_summary(
            PartialEmployee(), role_id="engineer", domains=[], project_id="p1"
        )
        assert summary["employee_id"] == "e1"
        assert summary["employee_name"] == ""

    def test_candidate_recommendation_name_none_safe(self):
        recruiter = self._recruiter()

        class NameLessCandidate:
            id = "t1"
            category = "engineering"
            domains = []

        rec = recruiter._make_candidate_recommendation(
            NameLessCandidate(), rationale="r", role_id="engineer"
        )
        assert rec.template_id == "t1"
        assert rec.template_name == ""

    def test_heuristic_direct_role_execution(self):
        recruiter = self._recruiter()
        item = {
            "need": RecruitmentNeed(role_id="viewer", role_name="Viewer"),
            "existing_employees": [],
            "candidates": [],
            "employee_pool": [],
            "triage_action": "direct_role_execution",
            "selected_categories": [],
            "category_rationale": "simple",
        }
        proposal = recruiter._heuristic_proposal_for_prepared_need(item, project_id="p1")
        assert proposal.status == "direct_role_execution"

    def test_heuristic_existing_staff_ignores_cross_role_pool(self):
        org = OrgEngine()
        org.register_employee(
            OrgEmployee(
                employee_id="e1",
                name="Alice",
                role_id="engineer",
                category="engineering",
                domains=["python"],
            )
        )
        org.register_employee(
            OrgEmployee(
                employee_id="e2",
                name="Bob",
                role_id="analyst",
                category="analytics",
                domains=["sql"],
                metadata={"completed_work_items": 50},
            )
        )
        recruiter = self._recruiter(org=org)
        item = {
            "need": RecruitmentNeed(role_id="engineer", role_name="Engineer"),
            "existing_employees": [],
            "employee_pool": [org.get_employee("e1"), org.get_employee("e2")],
            "candidates": [],
            "triage_action": "category_screening",
            "selected_categories": ["engineering"],
            "category_rationale": "screened",
        }
        proposal = recruiter._heuristic_proposal_for_prepared_need(item, project_id="p1")
        assert proposal.status != "existing_staff"
        assert proposal.existing_employee is None

    def test_employee_summary_uses_metadata_experience(self):
        recruiter = self._recruiter()
        employee = OrgEmployee(
            employee_id="e1",
            name="Alice",
            role_id="engineer",
            category="engineering",
            domains=["python"],
            metadata={"completed_work_items": 12, "learned_skill_refs": ["py:orm"]},
        )
        summary = recruiter._build_existing_employee_summary(
            employee, role_id="engineer", domains=[], project_id="p1"
        )
        assert summary["experience_score"] == 12.0
        assert "py:orm" in summary["learned_skill_refs"]

    def test_employee_summary_zero_without_signals(self):
        recruiter = self._recruiter()
        employee = OrgEmployee(employee_id="e1", name="Alice", role_id="engineer")
        summary = recruiter._build_existing_employee_summary(
            employee, role_id="engineer", domains=[], project_id="p1"
        )
        assert summary["experience_score"] == 0.0

    def test_build_triage_plan_from_response_invalid_action(self):
        with pytest.raises(ValueError):
            CompanyRecruiter._build_triage_plan_from_response(
                [{"role_id": "engineer", "action": "bogus", "categories": [], "rationale": ""}],
                expected_roles={"engineer"},
                valid_categories={"engineering"},
            )

    @pytest.mark.asyncio
    async def test_build_plan_llm_global_four_branches(self):
        agents = [
            SimpleNamespace(
                role_id="engineer",
                name="Engineer",
                reports_to="ceo",
                can_spawn=[],
                responsibility="coding",
            ),
            SimpleNamespace(
                role_id="analyst",
                name="Analyst",
                reports_to="ceo",
                can_spawn=[],
                responsibility="analysis",
            ),
        ]
        employee = OrgEmployee(
            employee_id="e1",
            name="Alice",
            role_id="engineer",
            category="engineering",
            domains=["python"],
        )
        org = _FakeOrg(agents, [employee])
        triage_json = json.dumps(
            {
                "roles": [
                    {
                        "role_id": "engineer",
                        "action": "category_screening",
                        "categories": ["engineering"],
                        "rationale": "needs code",
                    },
                    {
                        "role_id": "analyst",
                        "action": "category_screening",
                        "categories": ["analytics"],
                        "rationale": "needs data",
                    },
                ]
            }
        )
        global_json = json.dumps(
            {
                "proposals": [
                    {
                        "role_id": "engineer",
                        "status": "existing_staff",
                        "employee_id": "e1",
                        "template_id": "",
                        "proposed_employee_name": "",
                        "rationale": "has experience",
                    },
                    {
                        "role_id": "analyst",
                        "status": "proposed_hire",
                        "employee_id": "",
                        "template_id": "data_analyst",
                        "proposed_employee_name": "数据专员",
                        "rationale": "best fit",
                    },
                ]
            }
        )
        llm = _MockLLM([triage_json, global_json])
        recruiter = self._recruiter(org=org, llm=llm)
        plan = await recruiter.build_recruitment_plan(_FakeSpec(), domains=[], project_id="p1")
        by_role = {p.role_id: p for p in plan.proposals}
        assert by_role["engineer"].status == "existing_staff"
        assert by_role["engineer"].existing_employee.employee_id == "e1"
        assert by_role["engineer"].metadata["selection_source"] == "llm_global"
        assert by_role["analyst"].status == "proposed_hire"
        assert by_role["analyst"].candidate.template_id == "data_analyst"
        assert by_role["analyst"].candidate.proposed_employee_name == "数据专员"
        assert llm.calls[0][0]["role"] == "system"
        assert llm.calls[0][1]["content"].startswith("{")

    @pytest.mark.asyncio
    async def test_build_plan_llm_global_retries_then_heuristic(self):
        agents = [
            SimpleNamespace(
                role_id="engineer",
                name="Engineer",
                reports_to="ceo",
                can_spawn=[],
                responsibility="coding",
            ),
        ]
        org = _FakeOrg(agents, [])
        triage_json = json.dumps(
            {
                "roles": [
                    {
                        "role_id": "engineer",
                        "action": "category_screening",
                        "categories": ["engineering"],
                        "rationale": "needs code",
                    },
                ]
            }
        )
        bad_global = json.dumps({"proposals": []})
        llm = _MockLLM([triage_json, bad_global, bad_global, bad_global])
        recruiter = self._recruiter(org=org, llm=llm)
        plan = await recruiter.build_recruitment_plan(_FakeSpec(), domains=[], project_id="p1")
        assert plan.proposals
        assert "heuristic" in plan.proposals[0].metadata["selection_source"]

    @pytest.mark.asyncio
    async def test_build_plan_llm_global_duplicate_rejected(self):
        agents = [
            SimpleNamespace(
                role_id="engineer",
                name="Engineer",
                reports_to="ceo",
                can_spawn=[],
                responsibility="coding",
            ),
        ]
        org = _FakeOrg(agents, [])
        triage_json = json.dumps(
            {
                "roles": [
                    {
                        "role_id": "engineer",
                        "action": "category_screening",
                        "categories": ["engineering"],
                        "rationale": "needs code",
                    },
                ]
            }
        )
        duplicate_json = json.dumps(
            {
                "proposals": [
                    {
                        "role_id": "engineer",
                        "status": "proposed_hire",
                        "employee_id": "",
                        "template_id": "general_engineer",
                        "proposed_employee_name": "",
                        "rationale": "a",
                    },
                    {
                        "role_id": "engineer",
                        "status": "proposed_hire",
                        "employee_id": "",
                        "template_id": "general_engineer",
                        "proposed_employee_name": "",
                        "rationale": "b",
                    },
                ]
            }
        )
        llm = _MockLLM([triage_json, duplicate_json, duplicate_json, duplicate_json])
        recruiter = self._recruiter(org=org, llm=llm)
        plan = await recruiter.build_recruitment_plan(_FakeSpec(), domains=[], project_id="p1")
        assert "heuristic" in plan.proposals[0].metadata["selection_source"]

    def test_render_summary_proposed_hire_and_fallback(self):
        recruiter = self._recruiter()
        plan = RecruitmentPlan(
            company_profile="corporate",
            proposals=[
                RecruitmentProposal(
                    role_id="analyst",
                    status="proposed_hire",
                    rationale="best fit",
                    role_labels=["Analyst"],
                    candidate=RecruitmentCandidateRecommendation(
                        template_id="data_analyst",
                        template_name="数据分析师",
                        proposed_employee_name="数据专员",
                    ),
                ),
                RecruitmentProposal(
                    role_id="writer", status="fallback_role_only", rationale="no match"
                ),
            ],
            metadata={"execution_mode": "company_mode"},
        )
        summary = recruiter.render_recruitment_summary(plan)
        assert "analyst" in summary
        assert "数据专员" in summary
        assert "approve" in summary
        assert "deny" in summary

    def test_render_summary_no_proposals(self):
        recruiter = self._recruiter()
        summary = recruiter.render_recruitment_summary(
            RecruitmentPlan(
                company_profile="corporate",
                proposals=[],
                metadata={"execution_mode": "company_mode"},
            )
        )
        assert "No staffing action is needed" in summary

    def test_parse_llm_json_strips_fence(self):
        raw = '```json\n{"status": "proposed_hire"}\n```'
        assert CompanyRecruiter._parse_llm_json(raw) == {"status": "proposed_hire"}
