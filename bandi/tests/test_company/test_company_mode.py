from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig, DispatcherReport
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.phase_hooks import HookRegistry
from bandi.company.recruiter import Recruiter, RecruitStatus
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "dispatch.db"))


@pytest.fixture
def dispatcher(store):
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
    )


class TestDispatcherBasics:
    def test_empty_tick(self, dispatcher: CompanyDispatcher):
        report = asyncio.run(dispatcher.tick())
        assert isinstance(report, DispatcherReport)
        assert report.tick_id
        assert not report.has_errors()

    def test_tick_id_unique(self, dispatcher: CompanyDispatcher):
        r1 = asyncio.run(dispatcher.tick())
        r2 = asyncio.run(dispatcher.tick())
        assert r1.tick_id != r2.tick_id

    def test_disabled_tick(self, dispatcher: CompanyDispatcher):
        dispatcher.config.enable_auto_process = False
        item = create_work_item(title="skip", turn_type=WorkItemTurnType.EXECUTE)
        dispatcher.store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert len(report.processed) == 0


class TestDispatcherSeatManagement:
    def test_claim_success(self, dispatcher: CompanyDispatcher):
        assert dispatcher.claim("worker")
        assert dispatcher.is_claimed("worker")

    def test_claim_fail_no_seat(self, dispatcher: CompanyDispatcher):
        assert not dispatcher.claim("nonexistent")

    def test_claim_twice_fails(self, dispatcher: CompanyDispatcher):
        assert dispatcher.claim("worker")
        assert not dispatcher.claim("worker")

    def test_release(self, dispatcher: CompanyDispatcher):
        dispatcher.claim("worker")
        assert dispatcher.release("worker")
        assert not dispatcher.is_claimed("worker")

    def test_release_no_seat_returns_false(self, dispatcher: CompanyDispatcher):
        assert not dispatcher.release("nonexistent")

    def test_release_unclaimed_seat_is_idempotent(self, dispatcher: CompanyDispatcher):
        assert dispatcher.release("worker")
        assert not dispatcher.is_claimed("worker")
        seat = dispatcher.recruiter.get_seat("worker")
        assert seat is not None
        assert seat.status == RecruitStatus.AVAILABLE

    def test_release_all(self, dispatcher: CompanyDispatcher):
        org = dispatcher.org_engine
        org.register_role("w2", "Worker2", "helper")
        dispatcher.recruiter.register_seat("w2")
        dispatcher.claim("worker")
        dispatcher.claim("w2")
        dispatcher.release_all()
        assert not dispatcher.is_claimed("worker")
        assert not dispatcher.is_claimed("w2")


class TestDispatcherTick:
    def test_queued_transitions_to_ready(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        item = create_work_item(title="task1", turn_type=WorkItemTurnType.EXECUTE)
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.READY
        assert any(
            t[0] == item.id and t[1] == "queued" and t[2] == "ready" for t in report.transitions
        )

    def test_ready_item_claims_seat_and_runs(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="task2", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase in (Phase.RUNNING, Phase.AWAITING_MANAGER_REVIEW)
        assert "worker" in report.claimed
        assert item.id in report.processed

    def test_max_concurrent_honored(self, store: WorkItemStore, monkeypatch):
        org = OrgEngine()
        for i in range(5):
            org.register_role(f"w{i}", f"Worker{i}", f"helper{i}")
        recruiter = Recruiter()
        for i in range(5):
            recruiter.register_seat(f"w{i}")
        disp = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            gate_harness=GateHarness(),
            seat_executor=EngineSeatExecutor(),
            config=DispatcherConfig(max_concurrent=2),
        )
        for i in range(4):
            item = create_work_item(
                title=f"t{i}", turn_type=WorkItemTurnType.EXECUTE, assignee_role=f"w{i}"
            )
            store.save(item)

        probe = {"running": 0, "peak": 0}

        async def _fake_run_one(entry):
            probe["running"] += 1
            probe["peak"] = max(probe["peak"], probe["running"])
            await asyncio.sleep(0.05)
            probe["running"] -= 1
            return (*entry, True, "", 1.0)

        monkeypatch.setattr(disp, "_run_one", _fake_run_one)
        report = asyncio.run(disp.tick())
        # 并发闸门看「同时在跑」而非每 tick 总量：卡级接续允许同 tick 消费整队
        assert probe["peak"] <= 2
        assert len(report.processed) == 4

    def test_rework_items_reset(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        from bandi.company.work_item import create_work_item as cwi

        dispatcher.recruiter.register_seat("rework_role")
        dispatcher.org_engine.register_role("rework_role", "Reworker", "handles rework")
        item = cwi(title="rework", turn_type=WorkItemTurnType.EXECUTE, assignee_role="rework_role")
        store.save(item)
        item = store.get(item.id)
        item.phase = Phase.READY_FOR_REWORK
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.RUNNING
        assert any(t[0] == item.id and t[2] == "running" for t in report.transitions)

    def test_rework_deleted_item_does_not_crash_tick(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """
        _dispatch_rework_items 的 except/finally 不得再访问 item.assignee_role。"""
        from bandi.company.seat_executor import EngineSeatExecutor

        class DeletingExecutor(EngineSeatExecutor):
            async def run_turn(self, runtime, work_item):
                store.delete(work_item.id)
                return SeatResult(success=True, output="deleted during run")

        dispatcher.recruiter.register_seat("rework_role")
        dispatcher.org_engine.register_role("rework_role", "Reworker", "handles rework")
        dispatcher.seat_executor = DeletingExecutor()
        item = create_work_item(
            title="rework-del",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="rework_role",
        )
        store.save(item)
        item = store.get(item.id)
        item.phase = Phase.READY_FOR_REWORK
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert store.get(item.id) is None
        assert not report.errors

    def test_todo_deleted_item_does_not_crash_tick(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        from bandi.company.seat_executor import EngineSeatExecutor

        class DeletingExecutor(EngineSeatExecutor):
            async def run_turn(self, runtime, work_item):
                store.delete(work_item.id)
                return SeatResult(success=True, output="deleted during run")

        dispatcher.recruiter.register_seat("worker")
        dispatcher.org_engine.register_role("worker", "Worker", "handles work")
        dispatcher.seat_executor = DeletingExecutor()
        item = create_work_item(
            title="todo-del",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert item.id in report.processed
        assert store.get(item.id) is None


class TestDispatcherGateHarness:
    def test_passed_item_completes_without_reviewer(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="good", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        item.runtime_metadata["_last_output"] = "all done correctly"
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.APPROVED
        assert any(r[0] == item.id and r[1] == "pass" for r in report.gate_results)

    def test_empty_output_triggers_rework(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="empty", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        item.runtime_metadata["_last_output"] = ""
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.NEEDS_ATTENTION
        assert any(r[0] == item.id and r[1] == "rework" for r in report.gate_results)

    def test_blocker_triggers_escalation(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        item = create_work_item(
            title="blocked", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        item.runtime_metadata["_risks"] = ["blocker: no API key"]
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.NEEDS_ATTENTION
        assert any(r[0] == item.id and r[1] == "escalate" for r in report.gate_results)

    def test_contract_gate_skips_in_flight_and_leased_running(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """契约（执行中不误判·破坏者）：执行中（本进程 in-flight / 他进程有效租约）
        的 RUNNING 卡尚无 _last_output——gate 不得误判空输出降级（修复前直接
        REWORK→NEEDS_ATTENTION + 释放席位，打断真实执行）。"""
        from datetime import UTC, datetime, timedelta

        item1 = create_work_item(
            title="running-in-flight", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        store.save(item1)
        dispatcher._in_flight_work_item_ids.add(item1.id)

        item2 = create_work_item(
            title="running-leased", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        item2.metadata["execution_claim"] = {
            "instance_id": "other-instance",
            "lease_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        store.save(item2)

        report = asyncio.run(dispatcher.tick())

        assert store.get(item1.id).phase == Phase.RUNNING, "in-flight 卡不得被 gate 降级"
        assert store.get(item2.id).phase == Phase.RUNNING, "有效租约卡不得被 gate 降级"
        assert not any(r[0] in (item1.id, item2.id) for r in report.gate_results)

    def test_contract_run_work_item_releases_claim_on_success(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """契约（租约释放·破坏者）：执行完成后 execution_claim 必须释放——残留租约
        使 gate/恢复守卫（有效租约跳过）永久跳过该卡（卡死 RUNNING 无人推进）。"""
        item = create_work_item(
            title="lease release",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)
        assert dispatcher.claim("worker")
        asyncio.run(dispatcher.run_work_item(item.id))

        loaded = store.get(item.id)
        assert loaded is not None
        assert "execution_claim" not in (loaded.metadata or {}), (
            "执行结束必须释放租约（residual claim 阻塞 gate）"
        )

    def test_toplevel_default_reviewer_requires_active_employee(
        self, dispatcher: CompanyDispatcher
    ):
        """顶层卡默认评审角色需同时存在角色与在职员工；否则回落自动通过。"""
        from bandi.company.org_engine import OrgEmployee

        item = create_work_item(
            title="top level",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        assert dispatcher._resolve_reviewer_role(item) == ""
        dispatcher.org_engine.register_role("reviewer", "Reviewer")
        assert dispatcher._resolve_reviewer_role(item) == "", "无在职员工不得启用"
        dispatcher.org_engine.register_employee(
            OrgEmployee(employee_id="reviewer-1", name="Reviewer", role_id="reviewer")
        )
        assert dispatcher._resolve_reviewer_role(item) == "reviewer"

    def test_default_does_not_auto_approve_review(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """质量门默认生效：无评审意见的 AWAITING_MANAGER_REVIEW 卡不得自动通过。"""
        assert dispatcher.config.auto_approve_reviews is False
        item = create_work_item(
            title="needs review",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        assert store.get(item.id).phase == Phase.AWAITING_MANAGER_REVIEW

    def test_auto_approve_reviews_when_enabled(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """显式开启宽松自动化时，无裁决的评审卡才自动通过。"""
        dispatcher.config.auto_approve_reviews = True
        item = create_work_item(
            title="auto review",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        asyncio.run(dispatcher.tick())
        assert store.get(item.id).phase == Phase.APPROVED

    def test_review_decision_approve(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        item = create_work_item(
            title="review ok",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "approve"},
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.APPROVED

    def test_forced_additional_review_approve_goes_human_then_approved(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """reorg request_review 设置的 force_additional_review 标志必须被消费——
        approve 后转 AWAITING_HUMAN 强制人工二次评审，二次 approve 才 ；
        标志不得残留导致死循环。"""
        item = create_work_item(
            title="forced review",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "approve", "force_additional_review": True},
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None, "卡必须存在"
        assert loaded.phase == Phase.AWAITING_HUMAN, (
            f"force_additional_review 卡 approve 后必须强制人工二次评审，got {loaded.phase.value}"
        )
        assert "force_additional_review" not in (loaded.metadata or {}), (
            "标志必须消费（清 verdict 与标志），不得残留死循环"
        )
        dispatcher.review_work_item(item.id, "approve", feedback="human ok")
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED, (
            "人工二次 approve 后必须 APPROVED"
        )

    def test_review_work_item_rejects_non_review_phase(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """对非评审态卡（NEEDS_ATTENTION）写 review_verdict 必须拒绝——
        否则 tick 消费时 InvalidPhaseTransitionError 中断整轮调度。"""
        item = create_work_item(
            title="needs attention",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.NEEDS_ATTENTION,
        )
        store.save(item)
        with pytest.raises(ValueError, match="phase"):
            dispatcher.review_work_item(item.id, "approve")

    def test_review_work_item_on_attention_phase_does_not_break_tick(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """NEEDS_ATTENTION 卡若带 review_verdict（数据残留），tick 必须跳过
        而非抛异常中断整轮（其余工作项仍须推进）。"""
        item = create_work_item(
            title="needs attention",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.NEEDS_ATTENTION,
            metadata={"review_verdict": "approve"},
        )
        store.save(item)
        other = create_work_item(
            title="other task",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "approve"},
        )
        store.save(other)
        asyncio.run(dispatcher.tick())
        loaded_other = store.get(other.id)
        assert loaded_other is not None and loaded_other.phase == Phase.APPROVED, (
            "NEEDS_ATTENTION 卡的残留 verdict 不得中断其他卡的评审推进"
        )

    def test_review_work_item_write_surface_approve(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """review_work_item 是生产评审决策写入面——approve 后 tick 解除滞留。"""
        item = create_work_item(
            title="awaiting approval",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(item)
        dispatcher.review_work_item(item.id, "approve", feedback="looks good")
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED, (
            "review_work_item 写入的 verdict 必须被 tick 消费并解除滞留"
        )

    def test_review_work_item_write_surface_rework(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """rework 决策 + feedback 写入后 tick 转 READY_FOR_REWORK 且反馈保留。"""
        item = create_work_item(
            title="awaiting rework",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(item)
        dispatcher.review_work_item(item.id, "rework", feedback="fix the schema")
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.READY_FOR_REWORK
        assert loaded.metadata["rework_feedback"] == "fix the schema"
        assert loaded.metadata["review_rework_count"] == 1

    def test_review_work_item_rejects_unknown_verdict(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """未知 verdict 必须抛 ValueError（防静默滞留）。"""
        item = create_work_item(
            title="unknown", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.AWAITING_MANAGER_REVIEW
        )
        store.save(item)
        with pytest.raises(ValueError, match="approve"):
            dispatcher.review_work_item(item.id, "maybe")

    def test_review_work_item_missing_item_raises(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        with pytest.raises(KeyError, match="not found"):
            dispatcher.review_work_item("no-such-item", "approve")

    def test_review_rework_persists_feedback(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="needs rework",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "rework",
                "structured_review_verdict": {"reason": "fix the schema"},
            },
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.READY_FOR_REWORK
        assert loaded.metadata["review_rework_count"] == 1
        assert loaded.metadata["rework_feedback"] == "fix the schema"

    def test_review_verdict_consumed_on_approve(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """
        评审态时自动通过，绕过人工审核。"""
        item = create_work_item(
            title="approve then clear",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "approve", "structured_review_verdict": {"reason": "ok"}},
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert "review_verdict" not in (loaded.metadata or {}), "approve 后 verdict 必须消费清除"
        assert "structured_review_verdict" not in (loaded.metadata or {})

    def test_review_verdict_consumed_on_rework_breaks_infinite_loop(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """
        rework → 执行 → gate PASS → 终态：残留 verdict 必须被消费，不得再触发返工循环。"""
        item = create_work_item(
            title="rework then clear",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "rework", "structured_review_verdict": {"reason": "fix"}},
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.READY_FOR_REWORK
        assert "review_verdict" not in (loaded.metadata or {}), "rework 后 verdict 必须消费清除"
        # 闭环第二圈：rework 执行 → gate PASS → 直接终态（不再回评审态）
        item = store.get(item.id)
        assert item is not None
        from bandi.company.work_item import transition_work_item

        transition_work_item(item, Phase.RUNNING, store, reason="test: rework executed")
        item.runtime_metadata["_last_output"] = "fixed output"
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert "review_verdict" not in (loaded.metadata or {}), "gate PASS 后无残留 verdict"
        # 终态稳定：再 tick 不得回到返工/评审
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED, "终态卡不得被残留状态再次推进"

    def test_review_approve_clears_rework_residue(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """
        rework_feedback 置空 + review_rework_count 归零，杜绝"feedback 清了但 count 没清"的半残留态。"""
        item = create_work_item(
            title="rework then approve",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "approve",
                "rework_feedback": "old feedback",
                "review_rework_count": 2,
            },
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert not str(loaded.metadata.get("rework_feedback", "") or "").strip(), (
            "approve 后 rework_feedback 必须清空"
        )
        assert int(loaded.metadata.get("review_rework_count", 0) or 0) == 0, (
            "approve 后 review_rework_count 必须归零"
        )

    def test_rework_cap_reached_auto_done(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """
        rework cap 达成 auto_done 语义，并清空 feedback。"""
        dispatcher.config.max_review_reworks = 2
        item = create_work_item(
            title="cap me",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "rework",
                "structured_review_verdict": {"reason": "again"},
                "review_rework_count": 2,
            },
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED, (
            "rework 达上限必须 auto_done，不得再弹回 READY_FOR_REWORK"
        )
        assert not str(loaded.metadata.get("rework_feedback", "") or "").strip()
        assert bool(loaded.metadata.get("review_rework_cap_reached_auto_done", False))

    def test_transitions_recorded_in_report(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        approve_item = create_work_item(
            title="review approve",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "approve"},
        )
        rework_item = create_work_item(
            title="review rework",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={"review_verdict": "rework", "structured_review_verdict": {"reason": "fix"}},
        )
        store.save(approve_item)
        store.save(rework_item)
        report = asyncio.run(dispatcher.tick())
        transitions = {t[0]: t for t in report.transitions}
        assert transitions[approve_item.id][2] == "approved"
        assert transitions[rework_item.id][2] == "ready_for_rework"

    def test_get_report_returns_kanban_snapshot(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        store.save(create_work_item(title="t1", turn_type=WorkItemTurnType.EXECUTE))
        snapshot = dispatcher.get_report()
        assert snapshot.column_counts.get("todo", 0) >= 1
        assert not hasattr(snapshot, "tick_id")

    def test_build_reactivation_sweeper(self, store) -> None:
        from bandi.company.communication import CommunicationManager
        from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
        from bandi.company.gate_harness import GateHarness
        from bandi.company.reactivation_sweeper import CommsReactivationSweeper
        from bandi.company.recruiter import Recruiter

        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes")
        comm = CommunicationManager(workspace=".", project_id="p", session_id="s")
        d = CompanyDispatcher(
            store=store,
            recruiter=Recruiter(),
            org_engine=org,
            gate_harness=GateHarness(),
            communication=comm,
            config=DispatcherConfig(max_concurrent=2),
        )
        sweeper = d._build_reactivation_sweeper()
        assert isinstance(sweeper, CommsReactivationSweeper), "有 communication 必须构建 sweeper"
        assert sweeper.dispatcher is d
        assert sweeper.store is store

    def test_tick_drives_reactivation_sweeper(self, store) -> None:
        """修复：单次 tick（CLI/API 生产链路形态）必须驱动 sweeper 扫描——
        run_forever 全仓库零调用方，sweeper 仅接到 run_forever 等于未接线。"""
        import asyncio

        from bandi.company.communication import CommunicationManager
        from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
        from bandi.company.gate_harness import GateHarness
        from bandi.company.recruiter import Recruiter

        swept = {"n": 0}

        class _SweeperProbe:
            last_reactivation_report = []

            async def _tick(self) -> None:
                swept["n"] += 1

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes")
        comm = CommunicationManager(workspace=".", project_id="p", session_id="s")
        d = CompanyDispatcher(
            store=store,
            recruiter=Recruiter(),
            org_engine=org,
            gate_harness=GateHarness(),
            communication=comm,
            config=DispatcherConfig(max_concurrent=2),
        )
        d._build_reactivation_sweeper = lambda: _SweeperProbe()  # type: ignore[method-assign]
        asyncio.run(d.tick())
        assert swept["n"] >= 1, "tick 必须执行一次 sweeper 扫描（生产链路单次 tick 驱动）"


class TestDispatcherWakeEvent:
    def test_wake_event_cleared_by_default(self, dispatcher: CompanyDispatcher):
        assert not dispatcher.wake_event.is_set()

    def test_signal_wake_sets_event(self, dispatcher: CompanyDispatcher):
        dispatcher.signal_dispatcher_wake()
        assert dispatcher.wake_event.is_set()

    def test_wake_event_async_wait(self, dispatcher: CompanyDispatcher):
        async def waiter():
            dispatcher.signal_dispatcher_wake()
            await asyncio.wait_for(dispatcher.wake_event.wait(), timeout=1.0)
            return True

        result = asyncio.run(waiter())
        assert result


class TestDispatcherRunWorkItem:
    @pytest.mark.asyncio
    async def test_run_nonexistent(self, dispatcher: CompanyDispatcher):
        result = await dispatcher.run_work_item("nonexistent")
        assert not result.success
        assert "not found" in result.output

    @pytest.mark.asyncio
    async def test_run_existing(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher, tmp_path
    ):
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            item = create_work_item(
                title="exec me",
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="worker",
            )
            store.save(item)
            # 生产主链路 tick 即 claim→run，直接调用须对齐）
            assert dispatcher.claim("worker")
            result = await dispatcher.run_work_item(item.id)
        assert result.success
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.runtime_metadata.get("_task_status") == "completed"


class TestDispatcherReport:
    def test_report_elapsed(self, dispatcher: CompanyDispatcher):
        report = asyncio.run(dispatcher.tick())
        assert report.elapsed() >= 0.0

    def test_report_errors_list(self, dispatcher: CompanyDispatcher):
        report = asyncio.run(dispatcher.tick())
        assert isinstance(report.errors, list)

    def test_full_tick_lifecycle(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        org = dispatcher.org_engine
        org.register_role("reviewer", "Reviewer", "Reviews tasks", reports_to="worker")
        dispatcher.recruiter.register_seat("reviewer")

        item = create_work_item(
            title="full lifecycle",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        r1 = asyncio.run(dispatcher.tick())
        assert len(r1.transitions) >= 1

        loaded = store.get(item.id)
        if loaded and loaded.phase == Phase.RUNNING:
            loaded.runtime_metadata["_last_output"] = "full output ready"
            store.save(loaded)
            r2 = asyncio.run(dispatcher.tick())
            assert len(r2.gate_results) >= 1

    def test_hook_registry_integration(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        registry = HookRegistry()
        dispatcher.hook_registry = registry
        fired: list[str] = []

        def track(ctx):
            fired.append(ctx.work_item_id)

        registry.register("dispatcher_tick", track)
        asyncio.run(dispatcher.tick())
        assert len(fired) == 0

    def test_tick_actually_executes_work_item(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        calls: list[str] = []

        class FakeSeatExecutor:
            async def prepare_seat(self, runtime, work_item):
                pass

            async def run_turn(self, runtime, work_item):
                calls.append(work_item.id)
                return SeatResult(success=True, output="done")

            async def interrupt(self, runtime):
                pass

            async def shutdown(self):
                pass

        dispatcher.seat_executor = FakeSeatExecutor()
        item = create_work_item(
            title="exec", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
        asyncio.run(dispatcher.tick())
        assert item.id in calls

    def test_gate_rework_releases_seat(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        item = create_work_item(
            title="rework",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        item.runtime_metadata["_last_output"] = ""
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert any(t[0] == item.id and t[2] == "needs_attention" for t in report.transitions)
        assert "worker" in report.released
        seat = dispatcher.recruiter.get_seat("worker")
        assert seat is not None
        assert seat.status == RecruitStatus.AVAILABLE

    def test_crash_recovery_reschedules_stale_in_flight(self, store: WorkItemStore):
        """进程崩溃后（新 dispatcher 实例、无活跃执行登记），recover_stale_claims
        把 phase 停在 in-flight 的卡转 READY 重新调度。"""
        from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
        from bandi.company.gate_harness import GateHarness
        from bandi.company.org_engine import OrgEngine
        from bandi.company.recruiter import Recruiter

        item = create_work_item(
            title="stale running",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        store.save(item)

        # 模拟崩溃重启：全新 dispatcher（_in_flight_work_item_ids 为空）
        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        recruiter = Recruiter()
        recruiter.register_seat("worker")
        new_dispatcher = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            gate_harness=GateHarness(),
            config=DispatcherConfig(max_concurrent=3),
        )
        recovered = new_dispatcher.recover_stale_claims()
        assert recovered == 1, f"应恢复 1 张遗留卡，实际 {recovered}"
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.phase == Phase.READY, (
            f"崩溃遗留的 RUNNING 卡必须恢复为 READY，实际 {loaded.phase.value}"
        )

    def test_tick_recovers_stale_claim(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        """tick 本身必须做崩溃恢复（只接管租约已过期的卡），不能只靠启动流程。"""
        from datetime import UTC, datetime, timedelta

        dispatcher.config.enable_auto_process = False
        item = create_work_item(
            title="stale running",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        item.metadata["execution_claim"] = {
            "lease_expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        }
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert report.recovered_stale_claims == 1
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.READY

    def test_tick_does_not_recover_unclaimed_running(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """执行完、已释放租约但待 gate 的 RUNNING 卡不得被 tick 误恢复。"""
        dispatcher.config.enable_auto_process = False
        item = create_work_item(
            title="awaiting gate",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert report.recovered_stale_claims == 0
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.RUNNING

    def test_tick_reports_stagnant_work_items(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """tick 必须报告停滞卡（只报告不自动改状态），供 manager/人工分诊。"""
        from datetime import UTC, datetime, timedelta

        item = create_work_item(
            title="stuck dependency",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.WAITING_DEPENDENCIES,
            assignee_role="worker",
        )
        item.updated_at = datetime.now(UTC) - timedelta(hours=2)
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        ids = [entry["id"] for entry in report.stagnant_work_items]
        assert item.id in ids
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.WAITING_DEPENDENCIES

    def test_recover_skips_active_in_flight(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        """回归：活跃执行（已登记 in-flight）的卡不得被恢复。"""
        item = create_work_item(
            title="active running",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        store.save(item)
        dispatcher._in_flight_work_item_ids.add(item.id)
        recovered = dispatcher.recover_stale_claims()
        assert recovered == 0, "活跃执行中的卡不得被恢复"
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.RUNNING

    def test_recover_skips_review_phases(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        item = create_work_item(
            title="awaiting review",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        store.save(item)
        recovered = dispatcher.recover_stale_claims()
        assert recovered == 0
        loaded = store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.AWAITING_MANAGER_REVIEW

    def test_gate_replan_releases_seat(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        from bandi.company.gate_harness import GateResult, GateVerdict

        dispatcher.gate_harness.evaluate = lambda *a, **k: GateResult(
            GateVerdict.REPLAN, "wrong approach", []
        )
        item = create_work_item(
            title="replan",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert any(t[0] == item.id and t[2] == "needs_attention" for t in report.transitions)
        assert "worker" in report.released

    def test_gate_blocked_releases_seat(self, store: WorkItemStore, dispatcher: CompanyDispatcher):
        from bandi.company.gate_harness import GateResult, GateVerdict

        dispatcher.gate_harness.evaluate = lambda *a, **k: GateResult(
            GateVerdict.BLOCKED, "external blocker", []
        )
        item = create_work_item(
            title="blocked",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.RUNNING,
            assignee_role="worker",
        )
        store.save(item)
        report = asyncio.run(dispatcher.tick())
        assert any(t[0] == item.id and t[2] == "needs_attention" for t in report.transitions)
        assert "worker" in report.released

    def test_rework_transition_error_releases_seat(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        from unittest.mock import patch

        item = create_work_item(
            title="rework-error",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.READY_FOR_REWORK,
            assignee_role="worker",
        )
        store.save(item)
        with patch.object(store, "claim_for_execution", side_effect=RuntimeError("boom")):
            report = asyncio.run(dispatcher.tick())
        assert any(item.id in e and "rework" in e for e in report.errors)
        seat = dispatcher.recruiter.get_seat("worker")
        assert seat is not None
        assert seat.status == RecruitStatus.AVAILABLE


class TestDispatcherRefreshDependents:
    def test_tick_releases_waiting_parent_when_deps_approved(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        parent = create_work_item(
            title="parent",
            turn_type=WorkItemTurnType.AGGREGATE,
            metadata={"dependency_work_item_ids": ["child"]},
            phase=Phase.WAITING_DEPENDENCIES,
        )
        parent.id = "parent"
        child = create_work_item(
            title="child", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.APPROVED
        )
        child.id = "child"
        store.save(parent)
        store.save(child)
        asyncio.run(dispatcher.tick())
        refreshed = store.get("parent")
        assert refreshed is not None
        assert refreshed.phase == Phase.READY
        assert refreshed.metadata["waiting_on_work_item_ids"] == []

    def test_tick_keeps_parent_waiting_when_deps_incomplete(
        self, store: WorkItemStore, dispatcher: CompanyDispatcher
    ):
        parent = create_work_item(
            title="parent",
            turn_type=WorkItemTurnType.AGGREGATE,
            metadata={"dependency_work_item_ids": ["child"]},
            phase=Phase.WAITING_DEPENDENCIES,
        )
        parent.id = "parent"
        child = create_work_item(
            title="child", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        child.id = "child"
        store.save(parent)
        store.save(child)
        asyncio.run(dispatcher.tick())
        refreshed = store.get("parent")
        assert refreshed is not None
        assert refreshed.phase == Phase.WAITING_DEPENDENCIES
        assert refreshed.metadata["waiting_on_work_item_ids"] == ["child"]


class _StubExecutor:
    async def prepare_seat(self, runtime, item) -> None:
        return None

    async def run_turn(self, runtime, item) -> SeatResult:
        return SeatResult(success=True, output="ok")

    async def interrupt(self, runtime) -> None:
        return None


@pytest.fixture
def fusion_dispatcher(store) -> CompanyDispatcher:
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        seat_executor=_StubExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )


class TestDispatcherRoleQueueFusion:
    def test_runtime_lazily_assembled(self, dispatcher: CompanyDispatcher):
        runtime = dispatcher.runtime
        assert runtime.session_scope_id == "company-runtime"
        assert dispatcher.runtime is runtime

    def test_injected_runtime_is_used(self, store):
        from bandi.company.company_runtime import CompanyRuntime

        injected = CompanyRuntime(
            project_id="p",
            session_scope_id="injected",
            role_id="",
            employee_id="",
        )
        org = OrgEngine()
        org.register_role("worker", "Worker")
        recruiter = Recruiter()
        recruiter.register_seat("worker")
        d = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            runtime=injected,
        )
        assert d.runtime is injected

    def test_tick_dispatches_work_item_via_role_queue(
        self, store: WorkItemStore, fusion_dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="fusion",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)
        report = asyncio.run(fusion_dispatcher.tick())
        assert item.id in report.processed
        assert fusion_dispatcher.runtime.state.claimed_work_item_ids == set()
        refreshed = store.get(item.id)
        assert refreshed is not None
        assert refreshed.phase != Phase.QUEUED

    def test_tick_releases_seat_after_execution(
        self, store: WorkItemStore, fusion_dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="release", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
        asyncio.run(fusion_dispatcher.tick())
        assert not fusion_dispatcher.is_claimed("worker")
        assert fusion_dispatcher.runtime.state.claimed_work_item_ids == set()

    def test_tick_without_seat_executor_progresses_only(self, store: WorkItemStore):
        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        recruiter = Recruiter()
        recruiter.register_seat("worker")
        no_executor_dispatcher = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            config=DispatcherConfig(max_concurrent=3),
        )
        item = create_work_item(
            title="noexec", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
        report = asyncio.run(no_executor_dispatcher.tick())
        assert item.id not in report.processed
        refreshed = store.get(item.id)
        assert refreshed is not None
        assert refreshed.phase == Phase.READY
        assert no_executor_dispatcher.runtime.state.claimed_work_item_ids == set()
        assert not no_executor_dispatcher.is_claimed("worker")

    def test_tick_sessions_reset_after_run(
        self, store: WorkItemStore, fusion_dispatcher: CompanyDispatcher
    ):
        item = create_work_item(
            title="reset", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
        asyncio.run(fusion_dispatcher.tick())
        assert fusion_dispatcher.runtime.state.member_sessions == {}


class _ItemDeletingSeatExecutor:
    """执行时删除工作项的 seat executor——模拟执行期间 reorg 删卡
    （完整实现 SeatExecutor 接口，保证 run_work_item 正常完成走 finally）。"""

    def __init__(self, store: WorkItemStore, item_id: str):
        self._store = store
        self._item_id = item_id

    async def prepare_seat(self, runtime: Any, item: Any) -> None:
        return None

    async def run_turn(self, runtime: Any, item: Any) -> Any:
        from bandi.company.seat_executor import SeatResult

        self._store.delete(self._item_id)
        return SeatResult(success=True, output="done")

    async def interrupt(self, runtime: Any) -> None:
        return None

    async def checkpoint(self, runtime: Any) -> dict:
        return {}


@pytest.mark.asyncio
async def test_contract_seat_released_when_item_deleted_mid_run(store: WorkItemStore):
    """
    finally 用执行后重取的 item 判定会在 item=None 时短路泄漏座位。"""
    from bandi.company.work_item import create_work_item as cwi

    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    item = cwi(title="doomed", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker")
    store.save(item)

    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_ItemDeletingSeatExecutor(store, item.id),
        config=DispatcherConfig(max_concurrent=3),
    )
    await disp.tick()

    # 真实断言：claim 后的 seat 已释放（再次 claim 成功）
    assert disp.claim("worker"), "执行中删卡后 worker seat 未释放（finally 短路泄漏）"


def test_contract_recover_respects_active_lease(store: WorkItemStore):
    """
    存活证明在卡上的 execution_claim 租约，不在任何进程的内存集合。"""
    from datetime import UTC, datetime, timedelta

    org = OrgEngine()
    org.register_role("worker", "Worker", "t")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )
    item = create_work_item(
        title="busy",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.RUNNING,
        metadata={
            "execution_claim": {
                "instance_id": "other-instance",
                "claimed_at": datetime.now(UTC).isoformat(),
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=600)).isoformat(),
            }
        },
    )
    store.save(item)

    recovered = disp.recover_stale_claims()
    assert recovered == 0, f"有效租约被误恢复: {recovered}"
    loaded = store.get(item.id)
    assert loaded is not None and loaded.phase == Phase.RUNNING


def test_contract_recover_takes_over_expired_lease(store: WorkItemStore):
    from datetime import UTC, datetime, timedelta

    org = OrgEngine()
    org.register_role("worker", "Worker", "t")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )
    item = create_work_item(
        title="dead",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.RUNNING,
        metadata={
            "execution_claim": {
                "instance_id": "dead-instance",
                "claimed_at": (datetime.now(UTC) - timedelta(seconds=600)).isoformat(),
                "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=300)).isoformat(),
            }
        },
    )
    store.save(item)

    recovered = disp.recover_stale_claims()
    assert recovered == 1
    loaded = store.get(item.id)
    assert loaded is not None and loaded.phase == Phase.READY


def test_contract_dispatcher_writes_execution_claim_on_run(store: WorkItemStore):
    """
    卡 metadata（instance_id + lease），供其他进程的 recover 判定存活。"""
    import asyncio

    org = OrgEngine()
    org.register_role("worker", "Worker", "t")
    recruiter = Recruiter()
    recruiter.register_seat("worker")

    captured: dict = {}

    class _CaptureExec:
        async def prepare_seat(self, runtime: Any, item: Any) -> None:
            captured["claim"] = (store.get(item.id) or item).metadata.get("execution_claim")

        async def run_turn(self, runtime: Any, item: Any) -> Any:
            from bandi.company.seat_executor import SeatResult

            captured["claim"] = (store.get(item.id) or item).metadata.get("execution_claim")
            return SeatResult(success=True, output="ok")

        async def interrupt(self, runtime: Any) -> None:
            return None

        async def checkpoint(self, runtime: Any) -> dict:
            return {}

    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_CaptureExec(),
        config=DispatcherConfig(max_concurrent=3),
    )
    assert disp.instance_id, "dispatcher 缺少 instance_id"
    item = create_work_item(
        title="leased", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    asyncio.run(disp.tick())

    claim = captured.get("claim") or {}
    assert claim.get("instance_id") == disp.instance_id, f"租约未写 instance_id: {claim}"
    assert claim.get("lease_expires_at"), f"租约未写过期时间: {claim}"


def test_contract_recover_count_threshold_fails_repeated_crashes(store: WorkItemStore):
    """
    （承认结果未知，不盲目重试—。"""
    from datetime import UTC, datetime, timedelta

    org = OrgEngine()
    org.register_role("worker", "Worker", "t")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )
    item = create_work_item(
        title="crash-loop",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.RUNNING,
        metadata={
            # 已被恢复 3 次（达阈值），本次是第 4 次 → FAILED
            "recovery_count": 3,
            "execution_claim": {
                "instance_id": "dead",
                "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
            },
        },
    )
    store.save(item)

    disp.recover_stale_claims()
    loaded = store.get(item.id)
    assert loaded is not None and loaded.phase == Phase.FAILED, (
        f"重复崩溃卡未转 FAILED: {loaded.phase if loaded else None}"
    )
    assert "manual triage" in (loaded.metadata.get("transition_reason") or "") or (
        loaded.metadata.get("recovery_count", 0) >= 4
    )
