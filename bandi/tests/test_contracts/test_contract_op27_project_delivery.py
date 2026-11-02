# 功能：OP-27 契约——显式项目交付触发按员工反思与技能晋升（生产触发点接线）
# 契约原则：钉最终产物（档案反思计数 / 暂存 pending 引用 / 交付卡落库），不钉实现。
#
# 本文件的核心价值 = 用**真实 WorkItem**（项目归属只在 metadata）替代既有契约里的
# SimpleNamespace 假对象——后者带 `project_id` 属性，正是生产链路长期空转的假绿根源。
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from bandi.company.employee_evolution import EmployeeEvolutionManager
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.memory.staging import (
    MemoryPendingStore,
    get_skill_pending_store,
    reset_skill_pending_store,
)

_PENDING_PREFIX = "pending:"


@pytest.fixture()
def skill_store(tmp_path: Path) -> Iterator[MemoryPendingStore]:
    """功能：技能暂存区隔离（防污染真实 ~/.bandi 暂存库）。"""
    store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
    reset_skill_pending_store(store)
    yield store
    reset_skill_pending_store()


def _manager(tmp_path: Path) -> EmployeeEvolutionManager:
    return EmployeeEvolutionManager(home_dir=tmp_path, skills_dir=str(tmp_path / "skills"))


def _save_task(
    store: WorkItemStore,
    *,
    item_id: str,
    project_id: str,
    employee_id: str = "e1",
    role_id: str = "engineer",
    domains: list[str] | None = None,
    phase: Phase = Phase.APPROVED,
    summary: str = "done",
    title: str = "task",
):
    """功能：构造真实 WorkItem——项目归属仅落 metadata（与生产写入路径一致）。"""
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        description="desc",
        assignee_role=role_id,
        metadata={
            "project_id": project_id,
            "employee_assignment": {
                "employee_id": employee_id,
                "role_id": role_id,
                "name": employee_id,
                "domains": list(domains or []),
            },
            "work_item_summary_for_downstream": summary,
        },
        phase=phase,
    )
    item.id = item_id
    store.save(item)
    return item


def _pattern_count(manager: EmployeeEvolutionManager, pattern_key: str = "engineer|general") -> int:
    profile = manager.load_evolution_profile()
    return int(
        (
            profile.get("employees", {}).get("e1", {}).get("patterns", {}).get(pattern_key, {})
            or {}
        ).get("reflection_count", 0)
    )


class TestProjectIdResolution:
    """破坏者回归：真实 WorkItem 无 project_id 属性 ⇒ 必须回落 metadata。"""

    def test_real_work_item_resolves_project_id_from_metadata(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p-meta")
        delivery = create_work_item(
            title="项目交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p-meta"},
            phase=Phase.APPROVED,
        )

        assert not hasattr(delivery, "project_id"), "前提：WorkItem 不带 project_id 属性"
        assert getattr(delivery, "project_id", None) is None

        results = manager.record_project_reflections(delivery, [task])

        assert len(results) == 1, "必须从 metadata 解析项目归属并产生反思（旧实现恒 return []）"
        assert results[0]["employee_id"] == "e1"
        assert _pattern_count(manager) == 1


class TestDeliverProject:
    def test_deliver_records_reflection_and_stages_promotion(
        self, tmp_path: Path, skill_store
    ) -> None:
        """端到端：显式交付 → 反思入档案 → 二次交付达阈值 → 技能进暂存区待批准。"""
        from bandi.company.project_delivery import PROJECT_DELIVERY_KIND, deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))

        _save_task(store, item_id="wi-1", project_id="p1", summary="first")
        first = deliver_project(store=store, evolution=manager, project_id="p1")
        assert first["status"] == "recorded"
        assert first["item_ids"] == ["wi-1"]
        assert _pattern_count(manager) == 1
        assert not (tmp_path / "skills").exists(), "首次未达阈值：技能库零写入"

        delivery_card = next(
            item for item in store.list_all() if item.turn_type is WorkItemTurnType.DELIVER
        )
        assert delivery_card.metadata["kind"] == PROJECT_DELIVERY_KIND
        assert delivery_card.metadata["project_id"] == "p1"
        assert delivery_card.phase is Phase.APPROVED

        _save_task(store, item_id="wi-2", project_id="p1", summary="second")
        second = deliver_project(store=store, evolution=manager, project_id="p1")
        assert second["status"] == "recorded"
        assert _pattern_count(manager) == 2
        refs = [str(item.get("learned_skill_ref", "")) for item in second["reflections"]]
        assert any(ref.startswith(_PENDING_PREFIX) for ref in refs), "达阈值必须进暂存区（零直写）"
        assert not (tmp_path / "skills").exists(), "审批前技能库必须零写入"
        assert get_skill_pending_store().pending_count() == 1

    def test_deliver_without_items_is_noop(self, tmp_path: Path) -> None:
        """破坏者：无工作项的项目交付不得建卡、不得写档案。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))

        out = deliver_project(store=store, evolution=manager, project_id="empty")

        assert out["status"] == "no_work_items"
        assert out["reflections"] == []
        assert store.list_all() == [], "空项目不得留下交付卡"
        assert manager.load_evolution_profile().get("employees", {}) == {}

    def test_deliver_is_idempotent_without_new_items(self, tmp_path: Path) -> None:
        """契约：同一批工作项重复交付不得重复计数（防阈值提前达成）。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1")

        assert (
            deliver_project(store=store, evolution=manager, project_id="p1")["status"] == "recorded"
        )
        second = deliver_project(store=store, evolution=manager, project_id="p1")

        assert second["status"] == "already_delivered"
        assert second["item_ids"] == []
        assert _pattern_count(manager) == 1, "重复交付不得把 reflection_count 翻倍"

    def test_non_terminal_and_foreign_items_excluded(self, tmp_path: Path) -> None:
        """破坏者：未终态卡与他项目卡不得进入本次交付。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-run", project_id="p1", phase=Phase.RUNNING)
        _save_task(store, item_id="wi-other", project_id="p2")
        _save_task(store, item_id="wi-ok", project_id="p1")

        out = deliver_project(store=store, evolution=manager, project_id="p1")

        assert out["status"] == "recorded"
        assert out["item_ids"] == ["wi-ok"]

    @pytest.mark.parametrize(
        ("second_domains", "expected_key"),
        [([], "engineer|general"), (["backend"], "engineer|backend")],
    )
    def test_pattern_key_covers_domain_variants(
        self, tmp_path: Path, second_domains: list[str], expected_key: str
    ) -> None:
        """契约族：反思按「角色×领域」分档（不同 domains 落不同 pattern）。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1")
        _save_task(store, item_id="wi-2", project_id="p1", domains=second_domains)

        deliver_project(store=store, evolution=manager, project_id="p1")

        profile = manager.load_evolution_profile()
        patterns = profile["employees"]["e1"]["patterns"]
        assert expected_key in patterns

    def test_deliver_covers_multiple_employees(self, tmp_path: Path) -> None:
        """契约：一次交付覆盖项目内多个员工（各自独立计数）。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1", employee_id="e1")
        _save_task(store, item_id="wi-2", project_id="p1", employee_id="e2", role_id="reviewer")

        out = deliver_project(store=store, evolution=manager, project_id="p1")

        assert out["status"] == "recorded"
        assert {item["employee_id"] for item in out["reflections"]} == {"e1", "e2"}
        employees = manager.load_evolution_profile()["employees"]
        assert employees["e1"]["patterns"]["engineer|general"]["reflection_count"] == 1
        assert employees["e2"]["patterns"]["reviewer|general"]["reflection_count"] == 1

    def test_deliver_project_with_only_delivery_card_is_no_work_items(self, tmp_path: Path) -> None:
        """破坏者：项目里只有一张从未成功反思的交付卡 ⇒ 必须 `no_work_items`
        不得因把交付卡自己计入工作项而误报 `already_delivered`。"""
        from bandi.company.project_delivery import deliver_project

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        store.save(
            create_work_item(
                title="孤儿交付卡",
                turn_type=WorkItemTurnType.DELIVER,
                metadata={"project_id": "p-only", "kind": "project_delivery"},
                phase=Phase.APPROVED,
            )
        )

        out = deliver_project(store=store, evolution=manager, project_id="p-only")

        assert out["status"] == "no_work_items"
        assert out["reflections"] == []
        assert manager.load_evolution_profile().get("employees", {}) == {}

    def test_failed_reflection_keeps_items_retryable(self, tmp_path: Path, monkeypatch) -> None:
        """契约（失败安全）：反思中途失败 ⇒ 交付卡**不得**携带覆盖清单
        否则这批工作项会被永久排除出后续交付（静默丢失反思）；重试必须成功。"""
        from bandi.company.project_delivery import (
            REFLECTED_ITEM_IDS_KEY,
            deliver_project,
        )

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1")
        _save_task(store, item_id="wi-2", project_id="p1", summary="second")

        def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("staging failure")

        monkeypatch.setattr(manager, "record_project_reflections", _boom)
        with pytest.raises(RuntimeError):
            deliver_project(store=store, evolution=manager, project_id="p1")

        failed_card = next(
            item for item in store.list_all() if item.turn_type is WorkItemTurnType.DELIVER
        )
        assert not failed_card.metadata.get(REFLECTED_ITEM_IDS_KEY), (
            "反思失败的交付卡不得预写覆盖清单（否则这批项被永久跳过）"
        )
        assert not failed_card.metadata.get("reflection_recorded")

        # 恢复后重试：同一批工作项必须被重新收集并成功反思
        # （monkeypatch 的还原在 teardown 而非 with 块结束，须显式撤销）
        monkeypatch.undo()
        out = deliver_project(store=store, evolution=manager, project_id="p1")
        assert out["status"] == "recorded"
        assert out["item_ids"] == ["wi-1", "wi-2"]
        assert _pattern_count(manager) == 1
        retried_card = next(
            item
            for item in store.list_all()
            if item.turn_type is WorkItemTurnType.DELIVER and item.id != failed_card.id
        )
        assert sorted(retried_card.metadata[REFLECTED_ITEM_IDS_KEY]) == ["wi-1", "wi-2"]


class TestDeliveryCompletionHook:
    """交付卡经调度执行进入终态时的触发路径（前端入口后续 Day 由此进入）。"""

    def test_hook_ignores_non_delivery_cards(self, tmp_path: Path) -> None:
        from bandi.company.project_delivery import maybe_record_delivery_reflection

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        task = _save_task(store, item_id="wi-1", project_id="p1")

        assert maybe_record_delivery_reflection(store=store, evolution=manager, item=task) is None
        assert manager.load_evolution_profile().get("employees", {}) == {}

    def test_hook_fires_once_per_delivery_card(self, tmp_path: Path) -> None:
        from bandi.company.project_delivery import (
            PROJECT_DELIVERY_KIND,
            maybe_record_delivery_reflection,
        )

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1")
        card = create_work_item(
            title="项目交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p1", "kind": PROJECT_DELIVERY_KIND},
            phase=Phase.APPROVED,
        )
        store.save(card)

        first = maybe_record_delivery_reflection(store=store, evolution=manager, item=card)
        assert first is not None and first["status"] == "recorded"
        assert _pattern_count(manager) == 1

        again = maybe_record_delivery_reflection(store=store, evolution=manager, item=card)
        assert again is not None and again["status"] == "already_recorded"
        assert _pattern_count(manager) == 1, "同一交付卡不得重复计数"

    def test_hook_skips_non_terminal_delivery_card(self, tmp_path: Path) -> None:
        from bandi.company.project_delivery import (
            PROJECT_DELIVERY_KIND,
            maybe_record_delivery_reflection,
        )

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p1")
        card = create_work_item(
            title="项目交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={"project_id": "p1", "kind": PROJECT_DELIVERY_KIND},
            phase=Phase.RUNNING,
        )
        store.save(card)

        assert maybe_record_delivery_reflection(store=store, evolution=manager, item=card) is None
        assert _pattern_count(manager) == 0


class TestApprovalBoundary:
    """审批边界契约：交付卡经评审进入 （两条生产转换路径）同样触发反思晋升。

    修复前：钩子只在执行完成路径（`_record_work_item_completion`）触发，而交付卡
    停在 `AWAITING_MANAGER_REVIEW` 不算终态；审批通过后不再回到完成路径 ⇒ 静默不触发。
    """

    def _dispatcher(self, store: WorkItemStore, manager: EmployeeEvolutionManager):
        from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
        from bandi.company.gate_harness import GateHarness
        from bandi.company.org_engine import OrgEngine
        from bandi.company.recruiter import Recruiter
        from bandi.company.seat_executor import EngineSeatExecutor

        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        recruiter = Recruiter()
        recruiter.register_seat("worker")
        return CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            gate_harness=GateHarness(),
            seat_executor=EngineSeatExecutor(),
            config=DispatcherConfig(max_concurrent=3),
            employee_evolution=manager,
        )

    def test_reflection_fires_on_manager_approval(self, tmp_path: Path, skill_store) -> None:
        """路径 1：经理 approve ⇒ tick 消费 verdict 转 ⇒ 反思入档。"""
        from bandi.company.project_delivery import PROJECT_DELIVERY_KIND

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        dispatcher = self._dispatcher(store, manager)
        _save_task(store, item_id="wi-1", project_id="p1")
        card = create_work_item(
            title="项目交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={
                "project_id": "p1",
                "kind": PROJECT_DELIVERY_KIND,
                "review_verdict": "approve",
            },
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(card)

        dispatcher.review_work_item(card.id, "approve")
        asyncio.run(dispatcher.tick())

        loaded = store.get(card.id)
        assert loaded is not None and loaded.phase is Phase.APPROVED
        assert _pattern_count(manager) == 1, "审批通过的交付卡必须触发反思（审批边界）"

    def test_reflection_fires_on_rework_cap_auto_done(self, tmp_path: Path, skill_store) -> None:
        """路径 2（同族）：rework 达上限强制通过 ⇒ 同样触发。"""
        from bandi.company.project_delivery import PROJECT_DELIVERY_KIND

        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        dispatcher = self._dispatcher(store, manager)
        _save_task(store, item_id="wi-1", project_id="p1")
        card = create_work_item(
            title="项目交付",
            turn_type=WorkItemTurnType.DELIVER,
            metadata={
                "project_id": "p1",
                "kind": PROJECT_DELIVERY_KIND,
                "review_verdict": "rework",
                "review_rework_count": int(dispatcher.config.max_review_reworks),
            },
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(card)

        dispatcher.review_work_item(card.id, "rework")
        asyncio.run(dispatcher.tick())

        loaded = store.get(card.id)
        assert loaded is not None and loaded.phase is Phase.APPROVED, (
            "rework 达上限必须强制通过（auto-done）"
        )
        assert _pattern_count(manager) == 1, "同族转换路径不得漏触发"


class TestDeliveryEndpointWiring:
    def test_deliver_endpoint_registered(self) -> None:
        """接线：API 端点必须存在于生产路由表（前端入口后续 Day 接入）。"""
        import importlib

        app_mod = importlib.import_module("bandi.api.app")
        paths = {getattr(route, "path", "") for route in app_mod.app.routes}
        assert "/api/company/projects/{project_id}/deliver" in paths

    def test_endpoint_passes_dispatcher_store_and_evolution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """契约：端点必须把 dispatcher 的 store/evolution 透传给交付服务（参数透传面）。"""
        import asyncio
        import importlib
        from types import SimpleNamespace

        from bandi.company.projects import ProjectRecord, ProjectStore

        app_mod = importlib.import_module("bandi.api.app")
        manager = _manager(tmp_path)
        store = WorkItemStore(str(tmp_path / "wi.db"))
        _save_task(store, item_id="wi-1", project_id="p-api")

        project_store = ProjectStore(projects_dir=str(tmp_path / "projects"))
        project_store.save_project(ProjectRecord(project_id="p-api", name="API 项目"))
        monkeypatch.setattr(app_mod, "_get_project_store", lambda: project_store)
        monkeypatch.setattr(
            app_mod,
            "_get_company_dispatcher",
            lambda: SimpleNamespace(store=store, employee_evolution=manager),
        )

        out = asyncio.run(app_mod.company_project_deliver("p-api"))

        assert out["status"] == "recorded"
        assert out["item_ids"] == ["wi-1"]
        assert _pattern_count(manager) == 1, "端点必须真正把 dispatcher 的 store/evolution 传下去"

    def test_endpoint_rejects_unknown_project(self, tmp_path: Path, monkeypatch) -> None:
        """破坏者：项目不存在必须 404，且不得产出任何交付记录。"""
        import asyncio
        import importlib
        from types import SimpleNamespace

        from fastapi import HTTPException

        from bandi.company.projects import ProjectStore

        app_mod = importlib.import_module("bandi.api.app")
        store = WorkItemStore(str(tmp_path / "wi.db"))
        monkeypatch.setattr(
            app_mod,
            "_get_project_store",
            lambda: ProjectStore(projects_dir=str(tmp_path / "projects")),
        )
        monkeypatch.setattr(
            app_mod,
            "_get_company_dispatcher",
            lambda: SimpleNamespace(store=store, employee_evolution=_manager(tmp_path)),
        )

        with pytest.raises(HTTPException) as exc:
            asyncio.run(app_mod.company_project_deliver("ghost"))

        assert exc.value.status_code == 404
        assert store.list_all() == []
