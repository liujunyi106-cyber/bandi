"""端到端（Mock）：单 org 内双团队互 route 同时跑——调度隔离 + 执行会话隔离。

生产主链路：CompanyDispatcher.run_work_item（claim→run→终态），伪 SeatExecutor
记录实际执行的卡与运行时身份；团队归属由 org_engine role.team_id 权威解析
跨队 fail-closed（A 队角色会话不引用 B 队卡）。

断言「最终产物」：每队卡被各自归属 team 的执行会话完成、执行会话 key 互异
"""

from __future__ import annotations

import asyncio

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_executor import SeatResult
from bandi.company.seat_recruiter import Recruiter
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


class _CaptureExecutor:
    """伪席位执行器：记录每张卡实际执行时的运行时身份快照。"""

    def __init__(self) -> None:
        self.executed: list[dict[str, str]] = []

    async def prepare_seat(self, runtime, item) -> None:
        return None

    async def run_turn(self, runtime, item) -> SeatResult:
        self.executed.append(
            {
                "item_id": item.id,
                "team": runtime.team_instance_id,
                "session": runtime.member_session_id(),
                "home": str(sorted(runtime.state.home_team_instance_by_role.items())),
            }
        )
        return SeatResult(success=True, output="done", metadata={"terminal_reason": "VERIFIED"})

    async def interrupt(self, runtime) -> None:
        return None


def _mk_dispatcher(tmp_path) -> tuple[WorkItemStore, CompanyDispatcher, _CaptureExecutor]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    org.register_role(role_id="worker-a", name="A 队工人", team_id="team-a", reports_to=None)
    org.register_role(role_id="worker-b", name="B 队工人", team_id="team-b", reports_to=None)
    recruiter = Recruiter()
    recruiter.register_seat("worker-a")
    recruiter.register_seat("worker-b")
    executor = _CaptureExecutor()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        seat_executor=executor,
        config=DispatcherConfig(max_concurrent=3),
    )
    return store, dispatcher, executor


def _mk_card(title: str, role: str):
    card = create_work_item(title=title, turn_type=WorkItemTurnType.EXECUTE)
    card.assignee_role = role
    return card


def test_two_teams_route_simultaneously_execute_in_isolated_sessions(
    tmp_path,
) -> None:
    store, dispatcher, executor = _mk_dispatcher(tmp_path)
    card_a = _mk_card("A 队卡", "worker-a")
    card_b = _mk_card("B 队卡", "worker-b")
    store.save(card_a)
    store.save(card_b)
    assert dispatcher.claim("worker-a"), "A 队席位须可认领（生产主链路 claim→run）"
    assert dispatcher.claim("worker-b"), "B 队席位须可认领（生产主链路 claim→run）"

    result_a = asyncio.run(dispatcher.run_work_item(card_a.id))
    result_b = asyncio.run(dispatcher.run_work_item(card_b.id))
    assert result_a.success and result_b.success

    assert len(executor.executed) == 2
    exec_a = next(e for e in executor.executed if e["item_id"] == card_a.id)
    exec_b = next(e for e in executor.executed if e["item_id"] == card_b.id)
    assert exec_a["team"] == "team-a", "A 卡必须在 team-a 运行时身份下执行"
    assert exec_b["team"] == "team-b", "B 卡必须在 team-b 运行时身份下执行"
    assert exec_a["session"] != exec_b["session"], "两队执行会话必须互异（互 route 不串）"
    assert "team-a" in exec_a["session"] and "team-b" in exec_b["session"]

    phase_a = store.get(card_a.id)
    phase_b = store.get(card_b.id)
    assert phase_a is not None and phase_a.runtime_metadata.get("_task_status") == "completed"
    assert phase_b is not None and phase_b.runtime_metadata.get("_task_status") == "completed"


def test_home_team_map_injected_from_org_authority(tmp_path) -> None:
    _, dispatcher, _ = _mk_dispatcher(tmp_path)
    runtime = dispatcher.runtime
    assert runtime.state.home_team_instance_by_role == {"worker-a": "team-a", "worker-b": "team-b"}
    assert dispatcher._home_team_for_role("worker-a") == "team-a"
    assert dispatcher._home_team_for_role("worker-b") == "team-b"
    assert dispatcher._home_team_for_role("unknown") == ""
