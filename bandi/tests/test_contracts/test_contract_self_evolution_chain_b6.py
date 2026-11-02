"""契约族：自进化链（B6-A3/A4/A5/A6/7d）。

行为不变式：
1. **A3+A4 补丁流水线**：项目交付反思产出 → 自进化补丁事件 → 组织经验档案
   delta_profile 随之重算）；
2. **7d 质量维度**：完成时从评审/返工/失败信号抽取 strengths/weaknesses 传入
   `record_work_item_completion`（规则抽取 + LLM 注入接口）——计数不再恒空；
3. **A5+A6 桥接**：经验分与学到的技能写回组织员工 metadata——招募读取
   （`_metadata_experience_score` / `learned_skill_refs`）与前端档案
   （`employee.to_dict()["metadata"]`）同源可见；
4. **破坏者**：无员工/空档案/异常 → 桥接失败静默不炸链。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.company.employee_evolution import (
    DEFAULT_ORGANIZATION_ID,
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


class TestSelfEvolutionPatchPipeline:
    def test_reflection_applies_self_evolution_patch(self, tmp_path: Path) -> None:
        """"""
        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-a")
        delivery = create_work_item(
            title="交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-a"},
            phase=Phase.APPROVED,
        )

        reflections = manager.record_project_reflections(delivery, [task])
        assert len(reflections) == 1

        profile = manager.load_employee_experience(DEFAULT_ORGANIZATION_ID, "e1")
        events = profile.get("events") or []
        assert len(events) == 1, f"补丁事件应写入组织经验档案：{profile}"
        event = events[0]
        assert event["employee_id"] == "e1"
        assert event["strengths"], "strengths 应映射自 what_worked"
        assert event["avoid_next_time"], "avoid_next_time 应映射自 mistakes_to_avoid"
        assert event["source"].get("kind") == "project_reflection"
        assert event["evidence_task_ids"] == ["wi-1"]
        assert (profile.get("delta_profile") or {}).get("strengths"), "delta_profile 应重算"

    def test_patch_idempotent_on_repeat(self, tmp_path: Path) -> None:
        """幂等：同一交付同一员工重复反思——补丁事件只记一次（event_id 去重）。"""
        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-a")
        delivery = create_work_item(
            title="交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-a"},
            phase=Phase.APPROVED,
        )

        manager.record_project_reflections(delivery, [task])
        manager.record_project_reflections(delivery, [task])
        profile = manager.load_employee_experience(DEFAULT_ORGANIZATION_ID, "e1")
        assert len(profile.get("events") or []) == 1, "重复反思不得重复记补丁"


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
        delivery = create_work_item(
            title="交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-a"},
            phase=Phase.APPROVED,
        )
        manager.record_project_reflections(delivery, [task])

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


class TestApprovalChainSync:
    def test_approved_skill_syncs_to_org_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预期（批准闭环尾段）：技能批准回写档案后同步组织 metadata（前端可见）。"""
        import importlib
        from types import SimpleNamespace

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-a", phase=Phase.APPROVED)
        delivery = create_work_item(
            title="交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-a"},
            phase=Phase.APPROVED,
        )
        manager.record_project_reflections(delivery, [task])
        pattern_key = "engineer|general"

        org = _org_with_employee()
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_org_api_dispatcher", lambda: SimpleNamespace(org_engine=org))
        record = SimpleNamespace(
            payload={
                "profile_path": str(manager.evolution_profile_path()),
                "employee_id": "e1",
                "pattern_key": pattern_key,
                "skill_name": "交付规范",
            }
        )
        assert app_mod._write_back_approved_skill(record) is True

        employee = org.get_employee("e1")
        refs = list((employee.metadata or {}).get("learned_skill_refs") or [])
        assert "交付规范" in refs, f"批准后技能应同步进组织 metadata：{employee.metadata}"

    def test_write_back_syncs_from_payload_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """破坏者：批准回写的 metadata 同步必须读 payload 定位的档案（同源）——
        而不是进程默认 home（否则读空档案 → 经验分 0、技能引用空）。"""
        import importlib
        from types import SimpleNamespace

        manager_home = tmp_path / "evohome"
        manager = EmployeeEvolutionManager(home_dir=manager_home)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-a", phase=Phase.APPROVED)
        delivery = create_work_item(
            title="交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-a"},
            phase=Phase.APPROVED,
        )
        manager.record_project_reflections(delivery, [task])

        org = _org_with_employee()
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_org_api_dispatcher", lambda: SimpleNamespace(org_engine=org))
        record = SimpleNamespace(
            payload={
                "profile_path": str(manager.evolution_profile_path()),
                "employee_id": "e1",
                "pattern_key": "engineer|general",
                "skill_name": "交付规范X",
            }
        )
        assert app_mod._write_back_approved_skill(record) is True

        employee = org.get_employee("e1")
        meta = dict(employee.metadata or {})
        assert float(meta.get("experience_score") or 0.0) > 0, (
            f"经验分应从 payload 定位档案计算（修复前默认 home 读空档案得 0）：{meta}"
        )
        assert "交付规范X" in list(meta.get("learned_skill_refs") or []), (
            f"技能引用应从 payload 定位档案同步：{meta}"
        )
