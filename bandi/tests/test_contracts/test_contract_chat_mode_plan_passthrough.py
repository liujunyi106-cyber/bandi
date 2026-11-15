"""契约：群聊/单聊消息 mode 透传到员工执行（plan 商讨 / execute 执行）。

- handle_chat_message(mode="plan") → 卡 turn_type=PLAN（复用既有 plan 指南注入；
  群聊统一商讨语义：同一条消息对所有命中员工一致，plan 由前端会话级状态携带）；
- 单聊（target_role）plan 同样生效；
- seat_executor 对 PLAN 卡以 run_agent(mode="plan") 执行（规划轮），EXECUTE 保持；
- 破坏者：非法 mode 回落 EXECUTE（不崩、不误入 plan）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.api.company_chat import handle_chat_message
from bandi.company.communication import CommunicationManager
from bandi.company.company_runtime import CompanyRuntime
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _mk(tmp_path: Path) -> tuple[WorkItemStore, OrgEngine, CommunicationManager]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    org.register_role("worker-1", "worker-1")
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    return store, org, comm


class TestChatModeToTurnType:
    def test_plan_mode_creates_plan_item(self, tmp_path: Path) -> None:
        store, org, comm = _mk(tmp_path)
        created = handle_chat_message(
            content="我们要做一个新功能 @worker-1",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            mode="plan",
        )
        assert len(created) == 1
        assert created[0].turn_type == WorkItemTurnType.PLAN, (
            "群聊 plan 消息必须建 PLAN 卡（既有 plan 指南注入）"
        )

    def test_default_mode_is_execute(self, tmp_path: Path) -> None:
        store, org, comm = _mk(tmp_path)
        created = handle_chat_message(
            content="执行这个 @worker-1",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
        )
        assert created[0].turn_type == WorkItemTurnType.EXECUTE

    def test_invalid_mode_falls_back_to_execute(self, tmp_path: Path) -> None:
        store, org, comm = _mk(tmp_path)
        created = handle_chat_message(
            content="??? @worker-1",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            mode="hack",
        )
        assert created[0].turn_type == WorkItemTurnType.EXECUTE, (
            "非法 mode 必须回落 EXECUTE（不误入 plan）"
        )

    def test_dm_plan_mode_also_creates_plan_item(self, tmp_path: Path) -> None:
        store, org, comm = _mk(tmp_path)
        created = handle_chat_message(
            content="先规划一下",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
            target_role="worker-1",
            mode="plan",
        )
        assert len(created) == 1
        assert created[0].turn_type == WorkItemTurnType.PLAN, "单聊 plan 必须生效"


class TestSeatPassesModeToRunAgent:
    @pytest.mark.asyncio
    async def test_plan_item_runs_agent_in_plan_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        async def fake_run_agent(*, user_input: str, mode: str, **kwargs: object):
            captured["mode"] = mode
            return "plan output", "VERIFIED", 100, 1

        monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
        executor = EngineSeatExecutor()
        runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        item = create_work_item(title="plan this", turn_type=WorkItemTurnType.PLAN)
        await executor.run_turn(runtime, item)
        assert captured.get("mode") == "plan", (
            f"PLAN 卡必须以 plan 模式驱动员工（实际 {captured.get('mode')}）"
        )

    @pytest.mark.asyncio
    async def test_execute_item_still_runs_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        async def fake_run_agent(*, user_input: str, mode: str, **kwargs: object):
            captured["mode"] = mode
            return "done", "VERIFIED", 100, 1

        monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
        executor = EngineSeatExecutor()
        runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        item = create_work_item(title="run it", turn_type=WorkItemTurnType.EXECUTE)
        await executor.run_turn(runtime, item)
        assert captured.get("mode") == "execute"
