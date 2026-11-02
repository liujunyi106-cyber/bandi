# 功能：员工演进模块测试（经验追踪/项目反思/learned skill 晋升）
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bandi.company.employee_evolution import EmployeeEvolutionManager


@pytest.fixture
def manager(tmp_path: Path) -> EmployeeEvolutionManager:
    return EmployeeEvolutionManager(home_dir=tmp_path, skills_dir=str(tmp_path / "skills"))


def _task(
    task_id: str,
    *,
    title: str = "task",
    phase: str = "done",
    employee_id: str = "e1",
    role_id: str = "engineer",
    domains: list[str] | None = None,
    summary: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        title=title,
        phase=phase,
        status=phase,
        assignee_role=role_id,
        project_id="p1",
        tags=domains or [],
        metadata={
            "employee_assignment": {
                "employee_id": employee_id,
                "role_id": role_id,
                "name": "Alice",
                "template_id": "t1",
                "category": "engineering",
                "domains": domains or [],
            },
            "work_item_summary_for_downstream": summary,
        },
        result=None,
        assigned_external_agent="",
    )


class TestRecordCompletion:
    def test_records_global_and_project_profiles(self, manager: EmployeeEvolutionManager) -> None:
        task = _task("wi-1", summary="implemented feature X")
        result = manager.record_work_item_completion(task, "done output", outcome="success")

        assert result["employee_id"] == "e1"
        assert result["pattern_key"] == "engineer|general"

        global_profile = manager.load_evolution_profile()
        record = global_profile["employees"]["e1"]
        assert record["successes"] == 1
        assert record["roles"]["engineer"]["successes"] == 1
        assert record["last_outcome"] == "success"

        project_profile = manager.load_evolution_profile("p1")
        assert project_profile["employees"]["e1"]["successes"] == 1

    def test_outcome_normalization(self, manager: EmployeeEvolutionManager) -> None:
        task = _task("wi-2")
        manager.record_work_item_completion(task, "out", outcome="approved")
        manager.record_work_item_completion(task, "out", outcome="failed")
        manager.record_work_item_completion(task, "out", outcome="partial")

        record = manager.load_evolution_profile()["employees"]["e1"]
        assert record["successes"] == 1
        assert record["failures"] == 1
        assert record["partial_successes"] == 1

    def test_no_employee_assignment_skipped(self, manager: EmployeeEvolutionManager) -> None:
        task = SimpleNamespace(
            id="wi-x",
            title="x",
            phase="done",
            status="done",
            assignee_role="engineer",
            project_id="p1",
            tags=[],
            metadata={},
            result=None,
            assigned_external_agent="",
        )
        result = manager.record_work_item_completion(task, "out")
        assert result == {}


class TestExperienceScore:
    def test_score_counts_outcomes_and_bonuses(self, manager: EmployeeEvolutionManager) -> None:
        task = _task("wi-1", domains=["coding"])
        manager.record_work_item_completion(task, "ok", outcome="success")
        manager.record_work_item_completion(task, "ok", outcome="partial_success")

        score = manager.get_experience_score("e1", "engineer", domains=["coding"])
        # successes(1) + 0.5*partial(1) + 2*role_successes(1) + role_partial(1)
        # + domain(1 + 0.5) + reflection(0) + learned(0) = 1 + 0.5 + 2 + 1 + 1.5 = 6
        assert score == 6.0

    def test_score_min_zero(self, manager: EmployeeEvolutionManager) -> None:
        task = _task("wi-f", domains=[])
        manager.record_work_item_completion(task, "ok", outcome="failure")
        score = manager.get_experience_score("e1", "engineer")
        assert score == 0.0


class TestProjectReflections:
    def test_records_reflection_and_skips_nonterminal(
        self, manager: EmployeeEvolutionManager
    ) -> None:
        delivery = SimpleNamespace(
            id="del-1", project_id="p1", phase="done", status="done", metadata={}
        )
        done_task = _task("wi-1", summary="did A")
        running_task = _task("wi-2", phase="running", summary="in progress")

        results = manager.record_project_reflections(delivery, [done_task, running_task])
        assert len(results) == 1
        reflection = results[0]["reflection"]
        assert reflection["employee_id"] == "e1"
        assert reflection["delivery_task_id"] == "del-1"
        assert reflection["project_id"] == "p1"
        assert "what_worked" in reflection
        assert reflection["source_task_ids"] == ["wi-1"]

    def test_delta_context_built(self, manager: EmployeeEvolutionManager) -> None:
        delivery = SimpleNamespace(
            id="del-1", project_id="p1", phase="done", status="done", metadata={}
        )
        task = _task("wi-1", summary="did A")
        manager.record_project_reflections(delivery, [task])
        ctx = manager.build_employee_delta_context("e1", project_id="p1")
        assert "Reflected projects: 1" in ctx


class TestLearnedSkill:
    def test_promotes_after_threshold_into_staging(self, manager: EmployeeEvolutionManager) -> None:
        # 两次反思达阈值（LEARNED_SKILL_THRESHOLD=2），重复模式触发晋升——进暂存区待批准
        results: list[dict] = []
        for i in range(2):
            delivery = SimpleNamespace(
                id=f"del-{i}", project_id="p1", phase="done", status="done", metadata={}
            )
            task = _task(f"wi-{i}", summary=f"implemented feature {i}")
            results = manager.record_project_reflections(delivery, [task])

        learned = results[0].get("learned_skill_ref", "")
        assert learned.startswith("pending:"), "达阈值必须产生待批准技能引用"

        skill_dir = manager.home / "skills"
        assert not skill_dir.exists(), "审批前技能库必须零直写"

        from bandi.memory.staging import get_skill_pending_store

        records = get_skill_pending_store().list_pending()
        assert len(records) == 1
        assert records[0].origin == "background"
        assert records[0].subsystem == "skills"
        assert learned == f"pending:{records[0].id8}"

        # 审批前档案不得假装已学技能
        record = manager.load_evolution_profile()["employees"]["e1"]
        assert not record.get("learned_skill_refs"), "待批准技能不得记入档案引用"
        assert not record["patterns"]["engineer|general"].get("learned_skill_ref")


class TestSelfEvolutionPatch:
    def test_records_experience_event(self, manager: EmployeeEvolutionManager) -> None:
        recorded = manager.apply_employee_evolution_patch(
            organization_id="org1",
            patch={
                "patches": [
                    {
                        "employee_id": "e1",
                        "role_id": "engineer",
                        "summary": "improved handoff quality",
                        "strengths": ["clear handoffs"],
                        "adjustments": ["more tests"],
                    }
                ]
            },
            source={"checkpoint_id": "ck-1"},
        )
        assert len(recorded) == 1
        assert recorded[0]["status"] == "recorded"

        experience = manager.load_employee_experience("org1", "e1")
        assert experience["evolution_count"] == 1
        assert experience["delta_profile"]["strengths"] == ["clear handoffs"]
        ctx = manager.build_employee_delta_context("e1", organization_id="org1")
        assert "Self-evolution reviews: 1" in ctx

    def test_event_idempotent(self, manager: EmployeeEvolutionManager) -> None:
        patch_payload = {
            "patches": [
                {"employee_id": "e1", "event_id": "evt-1", "summary": "x", "role_id": "engineer"}
            ]
        }
        manager.apply_employee_evolution_patch(organization_id="org1", patch=patch_payload)
        second = manager.apply_employee_evolution_patch(organization_id="org1", patch=patch_payload)
        assert second == []
        experience = manager.load_employee_experience("org1", "e1")
        assert experience["evolution_count"] == 1

    def test_unallowed_employee_skipped(self, manager: EmployeeEvolutionManager) -> None:
        recorded = manager.apply_employee_evolution_patch(
            organization_id="org1",
            patch={"patches": [{"employee_id": "e2", "summary": "x", "role_id": "engineer"}]},
            source={},
            allowed_employee_ids={"e1"},
        )
        assert recorded == []


class TestPathsAndPersistence:
    def test_project_and_global_paths(
        self, manager: EmployeeEvolutionManager, tmp_path: Path
    ) -> None:
        assert (
            manager.evolution_profile_path()
            == tmp_path / "company" / "evolution" / "employees.json"
        )
        assert (
            manager.evolution_profile_path("p1")
            == tmp_path / "projects" / "p1" / "employee_evolution.json"
        )

    def test_safe_employee_filename(self) -> None:
        from bandi.company.employee_evolution import _safe_employee_filename

        assert _safe_employee_filename("a/b\\c") == "a-b-c"

    def test_corrupt_profile_returns_empty(
        self, manager: EmployeeEvolutionManager, tmp_path: Path
    ) -> None:
        path = tmp_path / "company" / "evolution" / "employees.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("corrupt json", encoding="utf-8")
        assert manager.load_evolution_profile() == {}


def test_contract_evolution_corrupt_profile_quarantined(tmp_path):
    """
    视图——不得在空档案上追加后覆盖清空历史；保存改原子写防截断。"""
    from bandi.company.employee_evolution import EmployeeEvolutionManager

    mgr = EmployeeEvolutionManager(home_dir=tmp_path / "evo")
    profile_path = mgr.evolution_profile_path("worker")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("{corrupt json!!", encoding="utf-8")

    data = mgr._load_json(profile_path)
    assert data == {}
    # 损坏被检测：原文件已隔离为 .corrupt
    corrupt_backups = list(profile_path.parent.glob("*.corrupt"))
    assert corrupt_backups, "损坏档案未被隔离备份"
    assert (
        not profile_path.exists() or profile_path.read_text(encoding="utf-8") != "{corrupt json!!"
    )
