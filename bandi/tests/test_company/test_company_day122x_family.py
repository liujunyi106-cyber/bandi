from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import (
    META_LAST_OUTPUT,
    META_TASK_STATUS,
    CompanyDispatcher,
    DispatcherConfig,
)
from bandi.company.company_runtime import (
    CompanyMemberSession,
    CompanyRuntime,
    CompanyRuntimeState,
)
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase, kanban_column
from bandi.company.phase_hooks import signal_dispatcher_hook
from bandi.company.recruiter import Recruiter
from bandi.company.runtime_identity import CompanyRuntimeIdentity
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.session_scoping import scoped_member_session_id
from bandi.company.store import WorkItemStore
from bandi.company.work_item import (
    WorkItem,
    WorkItemTurnType,
    create_work_item,
    transition_work_item,
)
from bandi.core.message import LLMResponse, ToolCall
from bandi.llm.mock import MockProvider

# （wake 接线 / settlement 兜底 / claim-release / run_forever / API 端点）


class _RecordingExecutor:
    def __init__(self) -> None:
        self.dispatched: list[Any] = []
        self.runtimes: list[Any] = []

    async def prepare_seat(self, runtime, work_item) -> None:
        pass

    async def run_turn(self, runtime, work_item) -> SeatResult:
        self.dispatched.append(work_item)
        self.runtimes.append(runtime)
        return SeatResult(success=True, output="done")

    async def checkpoint(self, runtime) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _build_dispatcher(
    tmp_path: Any, seat_executor: Any | None = None
) -> tuple[CompanyDispatcher, WorkItemStore]:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=seat_executor,
        config=DispatcherConfig(max_concurrent=3),
    )
    return disp, store


class TestWakeWiringE2E:
    def test_tick_transition_wakes_dispatcher_no_assignee(self, tmp_path: Any) -> None:
        """生产主链路：tick 产生真实转换 → hook 触发 signal_dispatcher_wake
        → dispatcher 的 wake 事件被置位（run_forever 得以继续循环）。"""
        disp, _store = _build_dispatcher(tmp_path)
        item = create_work_item(title="唤醒任务", turn_type=WorkItemTurnType.EXECUTE)
        disp.store.save(item)

        assert disp.wake_event.is_set() is False
        asyncio.run(disp.tick())
        # QUEUED→READY auto-progress 是真实转换，hook 必须置位 wake 事件
        assert disp.wake_event.is_set() is True, (
            "tick 的转换必须通过 hook 置位 dispatcher wake 事件"
        )

    def test_claim_release_cycle_through_tick(self, tmp_path: Any) -> None:
        """生产主链路：带 assignee 的工作项经 tick claim→run→release
        并在后续 tick 经真实 gate 流转到终态（无审批员即 APPROVED）。"""
        executor = _RecordingExecutor()
        disp, store = _build_dispatcher(tmp_path, executor)
        item = create_work_item(
            title="执行任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        # 第一轮 tick：claim→run→release（executor 收到真实 work_item）
        asyncio.run(disp.tick())

        assert len(executor.dispatched) == 1
        assert executor.dispatched[0].id == item.id
        runtime = executor.runtimes[0]
        assert runtime.role_id == "worker", "执行 runtime 必须带 assignee role"
        saved = store.get(item.id)
        assert saved is not None
        assert saved.runtime_metadata.get(META_TASK_STATUS) == "completed"
        assert saved.runtime_metadata.get(META_LAST_OUTPUT) == "done"

        # 第二轮 tick：真实 gate 评估（output 非空 + completed → PASS）
        # → 顶层卡没有审批员 ⇒ 直接 APPROVED（done 列）
        asyncio.run(disp.tick())
        saved = store.get(item.id)
        assert saved is not None
        assert saved.phase == Phase.APPROVED, (
            f"无审批员的卡 gate PASS 后应直接完成，实际 {saved.phase}"
        )
        assert kanban_column(saved.phase) == "done"
        assert disp.is_claimed("worker") is False, "gate 离开 RUNNING 后 seat 必须释放"


class TestSettlementE2E:
    def test_failure_triage_settles_in_tick(self, tmp_path: Any) -> None:
        """依赖先失败、父卡后创建：tick 兜底必须同 tick 收敛 failure-triage
        父卡离开 waiting 态并带印章 + turn_summary。"""
        disp, store = _build_dispatcher(tmp_path)
        child = create_work_item(title="子任务", turn_type=WorkItemTurnType.EXECUTE)
        store.save(child)
        transition_work_item(store.get(child.id), Phase.FAILED, store, reason="t")
        parent = create_work_item(
            title="父任务",
            turn_type=WorkItemTurnType.AGGREGATE,
            metadata={"dependency_work_item_ids": [child.id]},
        )
        store.save(parent)

        asyncio.run(disp.tick())
        asyncio.run(disp.tick())

        saved = store.get(parent.id)
        assert saved is not None
        assert saved.phase != Phase.WAITING_DEPENDENCIES, "tick 兜底必须 release 父卡"
        assert saved.metadata.get("dependency_settlement"), "必须带 settlement 印章"
        assert saved.metadata.get("turn_summary"), "必须写 turn_summary"


class TestRunForeverE2E:
    def test_run_forever_ticks_and_can_shutdown(self, tmp_path: Any) -> None:
        """run_forever 循环驱动 tick + shutdown 干净退出（wake 事件 + 信号）。"""
        executor = _RecordingExecutor()
        disp, store = _build_dispatcher(tmp_path, executor)
        item = create_work_item(
            title="后台任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        async def _driver() -> None:
            running = {"v": True}

            def _is_running() -> bool:
                return running["v"]

            task = asyncio.create_task(
                disp.run_forever(_is_running, wait_timeout_sec=0.05, loop_name="e2e")
            )
            await asyncio.sleep(0.3)
            running["v"] = False
            await asyncio.wait_for(task, timeout=2.0)
            await asyncio.sleep(0)
            disp.shutdown()

        asyncio.run(_driver())
        assert len(executor.dispatched) >= 1, "run_forever 必须至少完成一轮调度"
        assert disp.wake_event.is_set() is False, "shutdown 后 wake 事件必须被 unregister 复位"


class TestApiEndpointsE2E:
    def test_api_tick_then_kanban(self, tmp_path: Any) -> None:
        """API 主链路：post tick 驱动调度 → get kanban 反映列分组。"""
        import sys

        from fastapi.testclient import TestClient

        import bandi.api.app as _pkg

        del _pkg
        app_module = sys.modules["bandi.api.app"]
        app = app_module.app
        disp, store = _build_dispatcher(tmp_path)
        item = create_work_item(
            title="API任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        with patch.object(app_module, "_get_company_dispatcher", return_value=disp):
            with patch.object(app_module, "_API_KEY", "test-key"):
                with TestClient(app, headers={"Authorization": "Bearer test-key"}) as client:
                    r1 = client.post("/api/company/tick")
                    r2 = client.get("/api/company/kanban")

        assert r1.status_code == 200
        assert "tick_id" in r1.json()
        assert r2.status_code == 200
        body = r2.json()
        assert "column_counts" in body
        assert sum(body["column_counts"].values()) >= 1


# ==================== test_company_day122x_e2e_full ====================
# （EngineSeatExecutor → 真实 AgentLoop(MockProvider) 闭环 / 多卡并发轮转 /


def r1xe2efull_build_dispatcher(
    tmp_path: Any, seat_executor: Any, max_concurrent: int = 3
) -> tuple[CompanyDispatcher, WorkItemStore]:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=seat_executor,
        config=DispatcherConfig(max_concurrent=max_concurrent),
    )
    return disp, store


class TestRealExecutorE2E:
    def test_real_agent_loop_full_lifecycle(self, tmp_path: Any) -> None:
        """生产主链路：EngineSeatExecutor（真实 AgentLoop + MockProvider）经
        dispatcher tick 执行 → 结果写回 → 多 tick 流转到终态。"""
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor())
        item = create_work_item(
            title="整理项目周报",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        with patch("pathlib.Path.home", return_value=tmp_path):
            for _ in range(6):
                asyncio.run(disp.tick())
                saved = store.get(item.id)
                if saved is not None and saved.phase != Phase.RUNNING:
                    break

        saved = store.get(item.id)
        assert saved is not None
        # 真实 AgentLoop 产出非空 output + completed → gate PASS → 直接终态
        assert saved.runtime_metadata.get("_task_status") == "completed"
        assert saved.phase == Phase.APPROVED, f"无审批员的真实执行链应直接完成，实际 {saved.phase}"
        assert saved.runtime_metadata.get("_last_output"), "真实 AgentLoop 必须产生输出"
        assert disp.is_claimed("worker") is False, "真实执行后 seat 必须释放"


class TestMultiRoundRotationE2E:
    def test_multi_card_rotation_reaches_terminal(self, tmp_path: Any) -> None:
        """多卡并发轮转：多工作项经多 tick 全部离开 RUNNING，无 seat 泄漏。"""
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor(), max_concurrent=2)
        ids = []
        for i in range(4):
            item = create_work_item(
                title=f"并发任务{i}",
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="worker",
            )
            store.save(item)
            ids.append(item.id)

        with patch("pathlib.Path.home", return_value=tmp_path):
            for _ in range(12):
                asyncio.run(disp.tick())
                pending = [
                    i
                    for i in ids
                    if store.get(i).phase in (Phase.RUNNING, Phase.AWAITING_MANAGER_REVIEW)
                ]
                if not pending:
                    break

        # 全部离开 RUNNING（真实执行 + gate PASS → 评审无意见自动通过）
        phases = {store.get(i).phase for i in ids}
        assert all(p == Phase.APPROVED for p in phases), f"实际 {phases}"
        assert disp.is_claimed("worker") is False

    def test_max_concurrent_clamps_claims(self, tmp_path: Any) -> None:
        """max_concurrent 钳制：单 tick 内 claimed 数量不得超过上限。"""
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor(), max_concurrent=1)
        for i in range(4):
            item = create_work_item(
                title=f"钳制任务{i}",
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="worker",
            )
            store.save(item)

        with patch("pathlib.Path.home", return_value=tmp_path):
            report = asyncio.run(disp.tick())

        assert len(report.claimed) <= 1, f"claimed 必须 ≤ max_concurrent，实际 {report.claimed}"
        # 多轮后全部推进（钳制不阻塞后续轮）
        with patch("pathlib.Path.home", return_value=tmp_path):
            for _ in range(12):
                asyncio.run(disp.tick())
        all_items = store.list_all()
        assert all(i.phase != Phase.RUNNING for i in all_items)

    def test_wake_consumed_and_cleared_between_rounds(self, tmp_path: Any) -> None:
        """wake 事件消费复位：tick 后置位，run_forever 的 wait 消费后复位。"""
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor())
        item = create_work_item(
            title="唤醒轮转",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        with patch("pathlib.Path.home", return_value=tmp_path):
            asyncio.run(disp.tick())
            assert disp.wake_event.is_set() is True, "tick 产生转换必须置位 wake"
            # 模拟 run_forever 的 wait 消费：clear 后下一轮仍能再置位
            disp.wake_event.clear()
            for _ in range(6):
                asyncio.run(disp.tick())
            saved = store.get(item.id)
            assert saved is not None and saved.phase == Phase.APPROVED


class TestRotationTerminalAssertions:
    def test_completes_to_terminal_without_reviewer(self, tmp_path: Any) -> None:
        """完整轮转到终态：没有审批员（顶层卡）时 gate PASS 即 APPROVED（done 列）。"""
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor(), max_concurrent=1)
        item = create_work_item(
            title="终态任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(item)

        with patch("pathlib.Path.home", return_value=tmp_path):
            for _ in range(6):
                asyncio.run(disp.tick())
                saved = store.get(item.id)
                if saved is not None and saved.phase != Phase.RUNNING:
                    break

        final = store.get(item.id)
        assert final is not None
        assert final.phase == Phase.APPROVED, f"必须到达终态 APPROVED，实际 {final.phase}"
        assert kanban_column(final.phase) == "done"
        assert disp.is_claimed("worker") is False


class _CollabE2EHelper:
    """生产主链路：EngineSeatExecutor 注入 comms + 脚本化 MockProvider
    （AgentLoop 真实执行工具调用），断言最终落盘产物（inbox 消息文件）。"""

    @staticmethod
    def build_provider(tool_name: str, arguments: dict[str, Any]) -> MockProvider:
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="", tool_calls=[ToolCall(id="c1", name=tool_name, arguments=arguments)]
            )
        )
        provider.add_response(LLMResponse(content="done"))
        return provider

    @staticmethod
    def run_item_until_done(
        disp: CompanyDispatcher, store: WorkItemStore, item_id: str, tmp_path: Any
    ) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            for _ in range(6):
                asyncio.run(disp.tick())
                saved = store.get(item_id)
                if saved is not None and saved.phase != Phase.RUNNING:
                    break


class TestCollabToolsProductionE2E:
    def test_route_work_production_path(self, tmp_path: Any) -> None:
        """AgentLoop 真实调用 route_work（spawn_task）→ DM 落盘到目标角色
        inbox（.bandi-comms/.../inbox/manager/new/*.md），工作项正常完成。"""
        comms = CommunicationManager(workspace=str(tmp_path), project_id="p1", session_id="s1")
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor(communication=comms))
        item = create_work_item(
            title="整理季度财报",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={"ownership_contract": {"allowed_collaboration_targets": ["manager"]}},
        )
        store.save(item)

        provider = _CollabE2EHelper.build_provider(
            "route_work",
            {"action": "spawn_task", "target_role": "manager", "prompt": "请整理季度财报"},
        )
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            _CollabE2EHelper.run_item_until_done(disp, store, item.id, tmp_path)

        saved = store.get(item.id)
        assert saved is not None and saved.phase != Phase.RUNNING
        inbox_dir = tmp_path / ".bandi-comms" / "p1" / "s1" / "inbox" / "manager" / "new"
        messages = list(inbox_dir.glob("*.md"))
        assert messages, f"manager inbox 必须落盘 DM，实际 {inbox_dir}"
        body = messages[0].read_text(encoding="utf-8")
        assert "请整理季度财报" in body, f"DM body 必须含路由内容，实际 {body[:200]}"
        assert "worker" in body, f"DM 必须记录发送者 worker，实际 {body[:200]}"

    def test_close_human_review_production_path(self, tmp_path: Any) -> None:
        """AgentLoop 真实调用 close_human_review → 通知 DM 落盘到目标角色
        inbox，摘要与发送者写进消息体。"""
        comms = CommunicationManager(workspace=str(tmp_path), project_id="p1", session_id="s1")
        disp, store = r1xe2efull_build_dispatcher(tmp_path, EngineSeatExecutor(communication=comms))
        item = create_work_item(
            title="关闭评审",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={
                "ownership_contract": {"allowed_collaboration_targets": ["manager"]},
                # 授权（系统通道：work_item.metadata → ctx.meta → 工具 extra）
                "human_review_close_allowed": True,
            },
        )
        store.save(item)

        provider = _CollabE2EHelper.build_provider(
            "close_human_review",
            {"summary": "评审通过", "target_role": "manager"},
        )
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            _CollabE2EHelper.run_item_until_done(disp, store, item.id, tmp_path)

        saved = store.get(item.id)
        assert saved is not None and saved.phase != Phase.RUNNING
        inbox_dir = tmp_path / ".bandi-comms" / "p1" / "s1" / "inbox" / "manager" / "new"
        messages = list(inbox_dir.glob("*.md"))
        assert messages, f"manager inbox 必须落盘评审关闭通知，实际 {inbox_dir}"
        body = messages[0].read_text(encoding="utf-8")
        assert "评审通过" in body, f"通知 body 必须含摘要，实际 {body[:200]}"
        assert "Human review closed by worker" in body, f"通知必须记录关闭者，实际 {body[:200]}"


# ==================== test_company_day122x_w2 ====================


def r2xw2_build_dispatcher(tmp_path: Any, seat_executor: Any | None = None) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=seat_executor,
        config=DispatcherConfig(max_concurrent=3),
    )


class R2xw2RecordingExecutor:
    def __init__(self) -> None:
        self.runtimes: list[Any] = []

    async def prepare_seat(self, runtime, work_item) -> None:
        pass

    async def run_turn(self, runtime, work_item) -> SeatResult:
        self.runtimes.append(runtime)
        return SeatResult(success=True, output="done")

    async def checkpoint(self, runtime) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class TestSettlementTickFallback:
    def _seed_failure_then_late_card(self, disp: CompanyDispatcher) -> str:
        child = create_work_item(title="子任务", turn_type=WorkItemTurnType.EXECUTE)
        disp.store.save(child)
        transition_work_item(disp.store.get(child.id), Phase.FAILED, disp.store, reason="t")
        parent = create_work_item(
            title="父任务",
            turn_type=WorkItemTurnType.AGGREGATE,
            metadata={"dependency_work_item_ids": [child.id]},
        )
        disp.store.save(parent)
        return parent.id

    def test_tick_converges_failure_triage_in_same_tick(self, tmp_path: Any) -> None:
        """依赖先失败、卡后创建：无 hook 事件驱动 frontier，tick 兜底
        （has_pending_settlement_release 补一轮 refresh）必须在同一次 tick 内
        完成 failure-triage release，不留 WAITING_DEPENDENCIES 死图跨 tick 滞留。"""
        disp = r2xw2_build_dispatcher(tmp_path)
        parent_id = self._seed_failure_then_late_card(disp)

        asyncio.run(disp.tick())

        saved = disp.store.get(parent_id)
        assert saved is not None
        assert saved.phase != Phase.WAITING_DEPENDENCIES, (
            "tick 兜底必须在一次 tick 内 release 父卡，当前滞留 waiting_dependencies"
        )
        settlement = dict(saved.metadata.get("dependency_settlement", {}) or {})
        assert settlement, "release 必须带 dependency_settlement 印章"
        assert settlement.get("failed"), "印章必须记录失败依赖"

    def test_tick_failure_triage_writes_turn_summary(self, tmp_path: Any) -> None:
        """failure-triage release 时写 turn_summary（供 seat 生成分诊输入）。"""
        disp = r2xw2_build_dispatcher(tmp_path)
        parent_id = self._seed_failure_then_late_card(disp)

        asyncio.run(disp.tick())

        saved = disp.store.get(parent_id)
        assert saved is not None
        summary = saved.metadata.get("turn_summary", "")
        assert summary and "Triage" in summary, "failure-triage release 必须写 turn_summary"


class TestEffectiveExecutionAgent:
    def test_run_work_item_resolves_execution_agent(self, tmp_path: Any) -> None:
        """run_work_item 执行前必须解析最终执行 agent（native/external）
        并把解析结果写入 runtime_metadata（供 seat/external broker 消费）。"""
        executor = R2xw2RecordingExecutor()
        disp = r2xw2_build_dispatcher(tmp_path, executor)
        item = create_work_item(
            title="执行任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={"selected_execution_agent": "native"},
        )
        disp.store.save(item)

        # 生产主链路即 tick claim→run，直接调用须对齐）
        assert disp.claim("worker")
        result = asyncio.run(disp.run_work_item(item.id))
        assert result.success is True

        saved = disp.store.get(item.id)
        assert saved is not None
        assert saved.runtime_metadata.get("effective_execution_agent") == "native"
        assert saved.runtime_metadata.get("force_native_execution") is True

    def test_external_agent_resolution_written_to_metadata(self, tmp_path: Any) -> None:
        """preferred_external_agent 指定时解析结果必须写 external 指派。"""
        executor = R2xw2RecordingExecutor()
        disp = r2xw2_build_dispatcher(tmp_path, executor)
        item = create_work_item(
            title="外部任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={"preferred_external_agent": "codex"},
        )
        disp.store.save(item)

        asyncio.run(disp.run_work_item(item.id))

        saved = disp.store.get(item.id)
        assert saved is not None
        assert saved.runtime_metadata.get("assigned_external_agent") == "codex"
        assert saved.runtime_metadata.get("force_native_execution") is False


class TestTurnSummaryWiring:
    def test_synthesis_turn_summary_written_on_release(self, tmp_path: Any) -> None:
        """子任务全部 后，父卡 synthesis 释放时必须写 turn_summary
        （供 seat executor 生成执行输入）。"""
        disp = r2xw2_build_dispatcher(tmp_path)
        child_ids = []
        for cid in ("c1", "c2"):
            child = create_work_item(
                title=f"子任务{cid}",
                turn_type=WorkItemTurnType.EXECUTE,
                phase=Phase.APPROVED,
            )
            disp.store.save(child)
            child_ids.append(child.id)
        parent = create_work_item(
            title="汇总任务",
            turn_type=WorkItemTurnType.EXECUTE,
            phase=Phase.WAITING_FOR_CHILDREN,
            assignee_role="worker",
            metadata={
                "dependency_work_item_ids": child_ids,
                "delegated_children_pending": True,
            },
        )
        disp.store.save(parent)

        from bandi.company.work_item_transition import refresh_dependents

        changed = refresh_dependents(disp.store)
        assert changed is True

        saved = disp.store.get(parent.id)
        assert saved is not None
        summary = saved.metadata.get("turn_summary", "")
        assert summary and "Synthesize" in summary, "synthesis 释放必须写 turn_summary"


class TestPolicyTypeValidation:
    def test_capture_policy_validates_policy_type(self, tmp_path: Any) -> None:
        """capture_policy 必须用 PolicyType 校验枚举值（非法类型拒绝/回退）。"""
        from bandi.company.secretary import PolicyType, SecretaryService

        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))

        record = secretary.capture_policy(
            policy_type=PolicyType.RULE,
            content="先验证再合并",
            source="test",
        )
        assert record.policy_type == PolicyType.RULE

    def test_capture_policy_rejects_unknown_type(self, tmp_path: Any) -> None:
        from bandi.company.secretary import SecretaryService

        secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
        with pytest.raises(ValueError):
            secretary.capture_policy(
                policy_type="not-a-valid-type",
                content="非法类型",
                source="test",
            )


class TestEmployeeEvolutionWiring:
    def test_run_work_item_records_employee_evolution(self, tmp_path: Any) -> None:
        """
        profile（全局 + 项目）必须落盘——生产主链路最终产物断言。
        员工归属取 org 权威解析（角色下真实员工），不再用 role_id 冒充。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager
        from bandi.company.org_engine import OrgEmployee

        executor = R2xw2RecordingExecutor()
        store = WorkItemStore(str(tmp_path / "company.db"))
        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        org.register_employee(OrgEmployee(employee_id="E-100", name="Worker One", role_id="worker"))
        recruiter = Recruiter()
        recruiter.register_seat("worker")
        disp = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            gate_harness=GateHarness(),
            seat_executor=executor,
            config=DispatcherConfig(max_concurrent=3),
        )
        evolution = EmployeeEvolutionManager(home_dir=tmp_path)
        disp.employee_evolution = evolution

        item = create_work_item(
            title="员工演进接线任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        disp.store.save(item)

        assert disp.claim("worker")
        result = asyncio.run(disp.run_work_item(item.id))
        assert result.success is True

        global_profile = evolution.load_evolution_profile()
        assert global_profile["employees"]["E-100"]["successes"] == 1, (
            "work item 完成后全局演进 profile 必须记录真实员工经验（非角色 id）"
        )

        saved = disp.store.get(item.id)
        assert saved is not None
        assignment = dict(saved.metadata.get("employee_assignment", {}) or {})
        assert assignment.get("employee_id") == "E-100", (
            "dispatcher 必须写入真实员工 id（record 依赖它定位员工）"
        )

    def test_employee_evolution_skipped_without_assignee(self, tmp_path: Any) -> None:
        """无 assignee_role 的 work item 不得写入演进（employee_id 为空 → 静默跳过）。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        executor = R2xw2RecordingExecutor()
        store = WorkItemStore(str(tmp_path / "company.db"))
        org = OrgEngine()
        recruiter = Recruiter()
        disp = CompanyDispatcher(
            store=store,
            recruiter=recruiter,
            org_engine=org,
            gate_harness=GateHarness(),
            seat_executor=executor,
            config=DispatcherConfig(max_concurrent=3),
        )
        evolution = EmployeeEvolutionManager(home_dir=tmp_path)
        disp.employee_evolution = evolution

        item = create_work_item(
            title="无归属任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role=None,
        )
        disp.store.save(item)

        asyncio.run(disp.run_work_item(item.id))

        global_profile = evolution.load_evolution_profile()
        assert "employees" not in global_profile or global_profile["employees"] == {}, (
            "无 assignee 的 work item 不得写入演进 profile"
        )


# ==================== test_company_day122x_w3 ====================


def _make_runtime(project_id: str = "p1", role_id: str = "worker") -> CompanyRuntime:
    runtime = CompanyRuntime(
        project_id=project_id,
        session_scope_id=project_id,
        role_id=role_id,
        employee_id=role_id,
    )
    runtime.register_identity(
        CompanyRuntimeIdentity(
            project_id=project_id,
            session_scope_id=project_id,
            role_id=role_id,
            employee_id=role_id,
        )
    )
    return runtime


def _make_item(runtime: CompanyRuntime, title: str = "任务") -> WorkItem:
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=runtime.role_id,
    )
    runtime.push_to_queue(item)
    return item


class TestSessionFields:
    def test_new_session_has_default_fields(self) -> None:
        session = CompanyMemberSession(role_id="worker")
        assert session.background_work_item_ids == []
        assert session.manager_role_ids == []
        assert session.current_work_item == {}

    def test_claim_sets_current_work_item_snapshot(self) -> None:
        """claim 后 current_work_item 必须反映被认领的工作项（id + 类型快照）。"""
        runtime = _make_runtime()
        item = _make_item(runtime)

        assert runtime.claim_work_item(item.id, role_id="worker") is True

        session = runtime.state.member_sessions[runtime.member_session_id()]
        assert session.focused_work_item_id == item.id
        assert session.current_task_id == item.id
        snapshot = dict(session.current_work_item or {})
        assert snapshot.get("work_item_id") == item.id
        assert snapshot.get("task_id") == item.id

    def test_release_clears_current_work_item(self) -> None:
        """release 后 current_work_item 必须清空，与会话回到 idle 对称。"""
        runtime = _make_runtime()
        item = _make_item(runtime)
        runtime.claim_work_item(item.id, role_id="worker")

        assert runtime.release_work_item(item.id, role_id="worker") is True

        session = runtime.state.member_sessions[runtime.member_session_id()]
        assert session.status == "idle"
        assert session.focused_work_item_id == ""
        assert session.current_work_item == {}

    def test_manager_role_ids_accumulated(self) -> None:
        runtime = _make_runtime()
        item = create_work_item(
            title="带 manager 的任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={"manager_role_ids": ["lead", "lead"]},
        )
        runtime.push_to_queue(item)

        session = runtime.ensure_member_session()
        session.manager_role_ids = ["lead", "pm"]

        assert sorted(session.manager_role_ids) == ["lead", "pm"]


class TestBackgroundWorkItems:
    def test_background_work_item_ids_claimable_and_released(self) -> None:
        """background_work_item_ids 与 claim/release 语义独立：claim 改聚焦，"""
        runtime = _make_runtime()
        main = _make_item(runtime, "主任务")
        bg1 = _make_item(runtime, "后台任务1")
        bg2 = _make_item(runtime, "后台任务2")

        session = runtime.ensure_member_session()
        session.background_work_item_ids = [bg1.id, bg2.id]

        assert runtime.claim_work_item(main.id, role_id="worker") is True
        assert session.background_work_item_ids == [bg1.id, bg2.id]

        assert runtime.release_work_item(main.id, role_id="worker") is True
        assert session.background_work_item_ids == [bg1.id, bg2.id]
        assert session.focused_work_item_id == ""


class TestSessionScopeId:
    def test_role_session_resolved_through_claim(self) -> None:
        """多角色场景：claim 必须落到正确的角色会话（scope id 区分角色）。"""
        runtime = _make_runtime(project_id="p1", role_id="worker")
        item = _make_item(runtime)

        sid = scoped_member_session_id(
            project_id="p1",
            session_scope_id="p1",
            role_id="worker",
            employee_id="worker",
        )
        assert sid == runtime.member_session_id()

        assert runtime.claim_work_item(item.id, role_id="worker") is True
        session = runtime.state.member_sessions.get(sid)
        assert session is not None
        assert session.focused_work_item_id == item.id


class TestRuntimeStateClaims:
    def test_claimed_set_is_singleton(self) -> None:
        """claimed_work_item_ids 必须保持单值一致：claim 加入、release 移除。"""
        runtime = _make_runtime()
        item = _make_item(runtime)

        state = CompanyRuntimeState()
        runtime.use_state(state)
        try:
            runtime.claim_work_item(item.id, role_id="worker")
            assert item.id in state.claimed_work_item_ids
            runtime.release_work_item(item.id, role_id="worker")
            assert item.id not in state.claimed_work_item_ids
        finally:
            runtime._state_var.set(None)


# ==================== test_company_day122x_w4_api ====================


@contextmanager
def _company_client(tmp_path: Path):
    """隔离 dispatcher 存储到 tmp 目录，避免污染 ~/.bandi/company。"""
    import sys

    import bandi.api.app as _app_pkg
    from bandi.api.app import app

    del _app_pkg
    # bandi.api.__init__ 里 `from bandi.api.app import app` 覆盖了包属性
    # 必须用 sys.modules 取真实模块再 patch
    app_module = sys.modules["bandi.api.app"]

    company_dir = tmp_path / "company"
    company_dir.mkdir(exist_ok=True)

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(company_dir / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes work items")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=2),
    )
    with patch.object(app_module, "_get_company_dispatcher", return_value=dispatcher):
        with patch.object(app_module, "_API_KEY", "test-key"):
            with TestClient(app, headers={"Authorization": "Bearer test-key"}) as client:
                yield client, dispatcher


class TestCompanyTick:
    def test_tick_returns_report_shape(self, tmp_path: Path) -> None:
        with _company_client(tmp_path) as (client, _disp):
            resp = client.post("/api/company/tick")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tick_id"]
        assert "processed" in body
        assert "claimed" in body
        assert "released" in body
        assert "transitions" in body
        assert "errors" in body

    def test_tick_keeps_ready_item_without_executor(self, tmp_path: Path) -> None:
        """生产主链路：预置 READY 工作项 + 无 seat executor，tick 不消费不报错。"""
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        with _company_client(tmp_path) as (client, disp):
            disp.store.save(
                create_work_item(
                    title="待办任务",
                    turn_type=WorkItemTurnType.EXECUTE,
                    phase=Phase.READY,
                )
            )
            resp = client.post("/api/company/tick")
        assert resp.status_code == 200
        assert resp.json()["errors"] == []

    def test_tick_auto_progresses_queued_item(self, tmp_path: Path) -> None:
        """无 assignee 的 QUEUED 项在 tick 中 auto-progress 到 READY。"""
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        with _company_client(tmp_path) as (client, disp):
            item = create_work_item(
                title="排队任务", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.QUEUED
            )
            disp.store.save(item)
            resp = client.post("/api/company/tick")
            saved = disp.store.get(item.id)
        assert resp.status_code == 200
        assert saved is not None
        assert saved.phase == Phase.READY


class TestCompanyKanban:
    def test_kanban_returns_column_counts(self, tmp_path: Path) -> None:
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        with _company_client(tmp_path) as (client, disp):
            disp.store.save(
                create_work_item(
                    title="进行中", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
                )
            )
            resp = client.get("/api/company/kanban")
            counts = resp.json()["column_counts"]
        assert resp.status_code == 200
        assert sum(counts.values()) >= 1

    def test_kanban_empty_store(self, tmp_path: Path) -> None:
        with _company_client(tmp_path) as (client, _disp):
            resp = client.get("/api/company/kanban")
        assert resp.status_code == 200
        body = resp.json()
        assert "captured_at" in body
        assert "column_counts" in body


class TestCompanyDispatcherLoadsOrg:
    def test_api_dispatcher_loads_custom_roles_from_org_json(self, tmp_path: Path) -> None:
        """修复：API _get_company_dispatcher 必须加载 org.json 自定义角色——
        CLI add-role 持久化的组织在 API tick 链路必须可见（双入口一致）。"""
        import json
        import sys
        from unittest.mock import patch

        import bandi.api.app as _app_pkg

        del _app_pkg
        app_module = sys.modules["bandi.api.app"]

        company_dir = tmp_path / ".bandi" / "company"
        company_dir.mkdir(parents=True, exist_ok=True)
        org_json = company_dir / "org.json"
        org_json.write_text(
            json.dumps(
                {
                    "roles": [
                        {
                            "id": "designer",
                            "name": "Designer",
                            "reports_to": "ceo",
                            "can_spawn": [],
                            "permissions": {},
                        }
                    ],
                    "employees": [],
                    "seats": {"designer": "recruited"},
                }
            ),
            encoding="utf-8",
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(app_module, "_company_dispatcher", None),
        ):
            disp = app_module._get_company_dispatcher()
        assert disp.org_engine.get_role("designer") is not None, (
            "API dispatcher 必须加载 org.json 中的自定义角色（与 CLI 同一装配）"
        )


# ==================== test_company_day122x_wake ====================


def r5xwake_build_dispatcher(tmp_path: Any) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=3),
    )


class TestDispatcherWakeLinkage:
    def test_init_registers_wake_callback(self, tmp_path: Any) -> None:
        """CompanyDispatcher 构造时必须把自身 signal_dispatcher_wake 注册进
        phase_hooks，使 hook 能唤醒它。"""
        import bandi.company.phase_hooks as ph

        ph._DISPATCHER_WAKE_HOOKS.clear()
        disp = r5xwake_build_dispatcher(tmp_path)

        assert disp.signal_dispatcher_wake in ph._DISPATCHER_WAKE_HOOKS, (
            "构造后必须注册 dispatcher 的 wake 回调"
        )

    def test_signal_dispatcher_hook_sets_wake_event(self, tmp_path: Any) -> None:
        """signal_dispatcher_hook 对 runnable 目标 phase 触发时，必须 set 注册的 dispatcher
        wake_event（最终产物 = 事件被消费方可见）。"""
        import bandi.company.phase_hooks as ph

        ph._DISPATCHER_WAKE_HOOKS.clear()
        disp = r5xwake_build_dispatcher(tmp_path)
        assert not disp.wake_event.is_set()

        from datetime import UTC, datetime

        from bandi.company.phase import Phase

        ctx = ph.HookContext("w1", Phase.QUEUED, Phase.READY, "test", datetime.now(UTC))
        signal_dispatcher_hook(ctx)

        assert disp.wake_event.is_set(), "hook 触发后 dispatcher 的 wake_event 必须被 set"

    def test_tick_transition_wakes_dispatcher(self, tmp_path: Any) -> None:
        """生产主链路：带 assignee 的 item 经 tick 真实转换（QUEUED→READY）后
        dispatcher_tick hooks 执行并唤醒 dispatcher（wake_event 被 set）。"""
        import bandi.company.phase_hooks as ph

        ph._DISPATCHER_WAKE_HOOKS.clear()
        disp = r5xwake_build_dispatcher(tmp_path)
        item = create_work_item(
            title="整理报告",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        disp.store.save(item)

        import asyncio

        asyncio.run(disp.tick())

        saved = disp.store.get(item.id)
        assert saved is not None
        assert saved.phase != item.phase, "tick 必须让 item 离开 QUEUED"
        assert disp.wake_event.is_set(), "转换后 dispatcher 必须被唤醒"

    def test_default_hook_registry_has_five_hooks(self, tmp_path: Any) -> None:
        """CompanyDispatcher 构造后默认 hook_registry 非 None，且 dispatcher_tick
        组注册 5 个内置 hook（sync/signal/refresh/focus/enqueue）。"""
        disp = r5xwake_build_dispatcher(tmp_path)
        assert disp.hook_registry is not None, "构造后必须有默认 HookRegistry"
        hooks = disp.hook_registry._hooks.get("dispatcher_tick", [])
        assert len(hooks) == 5, f"dispatcher_tick 组必须注册 5 个 hook，实际 {len(hooks)}"

    def test_shutdown_unregisters_wake_callback(self, tmp_path: Any) -> None:
        """
        unregister_dispatcher_wake），避免多 dispatcher 互相唤醒。"""
        import bandi.company.phase_hooks as ph

        ph._DISPATCHER_WAKE_HOOKS.clear()
        disp = r5xwake_build_dispatcher(tmp_path)
        disp.shutdown()
        assert disp.signal_dispatcher_wake not in ph._DISPATCHER_WAKE_HOOKS
