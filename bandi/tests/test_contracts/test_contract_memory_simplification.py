# 功能：记忆体系精简契约——团队记忆/自动反思/自动晋升不得复活
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.phase import Phase
from bandi.company.project_delivery import COVERED_ITEM_IDS_KEY, deliver_project
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item


def _store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _done_task(store: WorkItemStore, *, project_id: str = "p1", item_id: str = "wi-1") -> WorkItem:
    item = create_work_item(
        title="任务",
        turn_type=WorkItemTurnType.EXECUTE,
        description="desc",
        assignee_role="engineer",
        metadata={
            "project_id": project_id,
            "employee_assignment": {"employee_id": "e1", "role_id": "engineer"},
        },
        phase=Phase.APPROVED,
    )
    item.id = item_id
    store.save(item)
    return item


class TestProjectDeliveryWithoutReflection:
    def test_delivery_records_coverage_and_no_reflections(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        task = _done_task(store)

        result = deliver_project(store=store, project_id="p1")

        assert result["status"] == "recorded"
        assert result["item_ids"] == [task.id]
        assert result["reflections"] == [], "交付不得再触发自动反思"
        delivery = store.get(result["delivery_item_id"])
        assert delivery is not None
        assert delivery.metadata[COVERED_ITEM_IDS_KEY] == [task.id]

    def test_repeat_delivery_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _done_task(store)

        assert deliver_project(store=store, project_id="p1")["status"] == "recorded"
        second = deliver_project(store=store, project_id="p1")

        assert second["status"] == "already_delivered"
        assert second["item_ids"] == []
        assert second["reflections"] == []

    def test_empty_project_reports_no_work_items(self, tmp_path: Path) -> None:
        result = deliver_project(store=_store(tmp_path), project_id="empty")

        assert result["status"] == "no_work_items"


class TestNoAutoSkillPromotion:
    def test_completion_does_not_stage_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company.employee_evolution import EmployeeEvolutionManager
        from bandi.memory.staging import get_skill_pending_store, reset_pending_store

        monkeypatch.setenv("BANDI_MEMORY_PENDING_DIR", str(tmp_path / "pending"))
        reset_pending_store()
        try:
            manager = EmployeeEvolutionManager(home_dir=tmp_path)
            store = _store(tmp_path)
            task = _done_task(store)

            for _ in range(3):
                manager.record_work_item_completion(task, "done", outcome="success")

            profile = manager.load_evolution_profile()
            assert profile["employees"]["e1"]["successes"] == 3, "经验记录必须保留"
            assert get_skill_pending_store().pending_count() == 0, (
                "完成工作项不得再产生技能待批准记录"
            )
        finally:
            reset_pending_store()


class TestRemovedApisDoNotReappear:
    def test_team_and_skill_pending_paths_absent(self) -> None:
        from bandi.api.app import app

        paths = app.openapi().get("paths", {})
        assert not any("team-pending" in path for path in paths)
        assert not any("skills/pending" in path for path in paths)
