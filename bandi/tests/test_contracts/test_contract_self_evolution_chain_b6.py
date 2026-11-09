"""契约族：自进化链（B6-A4/A5/A6/7d）。

行为不变式：
1. **7d 质量维度**：完成时从评审/返工/失败信号抽取 strengths/weaknesses 传入
   `record_work_item_completion`（规则抽取 + LLM 注入接口）——计数不再恒空；
2. **A5+A6 桥接**：经验分与学到的技能写回组织员工 metadata——招募读取
   （`_metadata_experience_score` / `learned_skill_refs`）与前端档案
   （`employee.to_dict()["metadata"]`）同源可见；
3. **破坏者**：无员工/空档案/异常 → 桥接失败静默不炸链。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.company.employee_evolution import (
    EmployeeEvolutionManager,
    extract_quality_dimensions,
)
from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _manager(tmp_path: Path) -> EmployeeEvolutionManager:
    return EmployeeEvolutionManager(home_dir=tmp_path)


def _org_with_employee(employee_id: str = "e1", role_id: str = "engineer") -> OrgEngine:
    org = OrgEngine()
    org.register_role(role_id, "Engineer", "builds")
    org.register_employee(
        OrgEmployee(employee_id=employee_id, name="小工", role_id=role_id, domains=["coding"])
    )
    return org


def _save_task(
    store: WorkItemStore,
    *,
    item_id: str,
    project_id: str,
    employee_id: str = "e1",
    role_id: str = "engineer",
    phase: Phase = Phase.APPROVED,
    summary: str = "done",
) -> Any:
    item = create_work_item(
        title="task",
        turn_type=WorkItemTurnType.EXECUTE,
        description="desc",
        assignee_role=role_id,
        metadata={
            "project_id": project_id,
            "employee_assignment": {
                "employee_id": employee_id,
                "role_id": role_id,
                "name": employee_id,
                "domains": ["coding"],
            },
            "work_item_summary_for_downstream": summary,
        },
        phase=phase,
    )
    item.id = item_id
    store.save(item)
    return item


class TestQualityDimensions:
    def test_rule_path_from_review_signals(self) -> None:
        """预期（规则路径）：approve 理由 → strengths；rework/失败信号 → weaknesses。"""
        dims = extract_quality_dimensions(
            {"structured_review_verdict": {"verdict": "approve", "reason": "交付质量高"}}
        )
        assert "交付质量高" in dims["strengths"]
        assert dims["rationale"] == "review:approve"

        dims2 = extract_quality_dimensions(
            {
                "structured_review_verdict": {"verdict": "rework", "reason": "缺少验证证据"},
                "rework_feedback": "补测试再交",
            }
        )
        assert "缺少验证证据" in dims2["weaknesses"]
        assert "补测试再交" in dims2["weaknesses"]

        dims3 = extract_quality_dimensions({"failure_reason": "命令超时"})
        assert "命令超时" in dims3["weaknesses"]

    def test_llm_injection_used_when_provided(self) -> None:
        """LLM 注入接口：提供 llm 时走 LLM 抽取（失败回落规则）。"""
        payloads: list[str] = []

        def _llm(prompt: str) -> str:
            payloads.append(prompt)
            return '{"strengths": ["条理清晰"], "weaknesses": ["边界遗漏"], "rationale": "llm"}'

        dims = extract_quality_dimensions(
            {"structured_review_verdict": {"verdict": "approve", "reason": "好"}}, llm=_llm
        )
        assert payloads, "llm 应被调用"
        assert dims["strengths"] == ["条理清晰"]
        assert dims["weaknesses"] == ["边界遗漏"]

    def test_llm_failure_falls_back_to_rules(self) -> None:
        def _boom(prompt: str) -> str:
            raise RuntimeError("llm down")

        dims = extract_quality_dimensions(
            {"structured_review_verdict": {"verdict": "approve", "reason": "稳"}}, llm=_boom
        )
        assert "稳" in dims["strengths"]

    def test_empty_metadata_yields_empty_dimensions(self) -> None:
        dims = extract_quality_dimensions({})
        assert dims["strengths"] == [] and dims["weaknesses"] == []


class TestCompletionFillsQualityDimensions:
    def test_completion_records_strengths_into_profile(self, tmp_path: Path) -> None:
        """预期（生产钩子）：完成带评审理由的卡 → 经验计数不再恒空。"""
        from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
        from bandi.company.gate_harness import GateHarness
        from bandi.company.recruiter import Recruiter
        from bandi.company.seat_executor import SeatResult

        store = WorkItemStore(str(tmp_path / "wi.db"))
        org = _org_with_employee()
        manager = _manager(tmp_path)
        dispatcher = CompanyDispatcher(
            store=store,
            recruiter=Recruiter(),
            org_engine=org,
            gate_harness=GateHarness(),
            config=DispatcherConfig(),
            employee_evolution=manager,
        )
        item = _save_task(store, item_id="wi-q", project_id="p-a")
        item.metadata["structured_review_verdict"] = {"verdict": "approve", "reason": "交付规范"}
        store.save(item)

        dispatcher._record_work_item_completion(item, SeatResult(success=True, output="done"))

        profile = manager.load_evolution_profile()
        counts = profile.get("employees", {}).get("e1", {}).get("working_pattern_counts", {}) or {}
        assert counts, f"strengths 应进入 working_pattern_counts：{profile}"
        assert any("交付规范" in key for key in counts), f"评审理由应落档：{counts}"


class TestOrgMetadataBridge:
    def test_sync_writes_score_and_skills(self, tmp_path: Path) -> None:
        """预期：同步把经验分与学到的技能写回组织员工 metadata（招募/档案同源）。"""
        manager = _manager(tmp_path)
        org = _org_with_employee()
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-a", phase=Phase.APPROVED)
        manager.record_work_item_completion(task, "done", outcome="success")

        assert manager.sync_org_employee_metadata(org, "e1") is True
        employee = org.get_employee("e1")
        assert employee is not None
        metadata = dict(employee.metadata or {})
        assert float(metadata.get("experience_score") or 0.0) > 0, (
            f"经验分应写回 metadata：{metadata}"
        )

        from bandi.company.recruiter import CompanyRecruiter

        assert CompanyRecruiter._metadata_experience_score(metadata) == float(
            metadata["experience_score"]
        ), "招募读取口径与写入同源"
        # 前端档案同源：to_dict 携带 metadata
        assert "experience_score" in employee.to_dict()["metadata"]

    def test_sync_fail_safe_when_employee_missing(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        org = _org_with_employee()
        assert manager.sync_org_employee_metadata(org, "no-such") is False
