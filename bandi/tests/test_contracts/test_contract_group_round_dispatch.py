# 功能：契约——Dispatcher 执行群聊卡后推进回合（懒建下一张、PASS 通过 gate、完成收口）。
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.api.company_chat import handle_chat_message
from bandi.company.company_mode import CompanyDispatcher
from bandi.company.group_round import GroupRoundService, RoundStatus
from bandi.company.group_round_store import GroupRoundStore
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_executor import SeatResult
from bandi.company.seat_recruiter import Recruiter
from bandi.company.store import WorkItemStore


class _Comms:
    def send_dm(self, **kwargs: Any) -> None:
        return None


class _FakeSeat:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        return None

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        self.calls.append(str(work_item.assignee_role or ""))
        return SeatResult(
            success=True,
            output=self.outputs.get(str(work_item.assignee_role or ""), "完成"),
        )

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _build(
    tmp_path: Path, outputs: dict[str, str]
) -> tuple[CompanyDispatcher, GroupRoundService, WorkItemStore, OrgEngine, _FakeSeat]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    recruiter = Recruiter()
    for role_id in ("lead", "worker"):
        org.register_role(role_id, role_id)
        recruiter.register_seat(role_id)
        recruiter.recruit(role_id)
    seat = _FakeSeat(outputs)
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        seat_executor=seat,
    )
    service = GroupRoundService(
        GroupRoundStore(str(tmp_path / "rounds.db")),
        store,
        org,
        mention_resolver=lambda text, members: [
            role for role in members if f"@{role}" in (text or "")
        ],
    )
    dispatcher.group_rounds = service
    return dispatcher, service, store, org, seat


def _submit(
    service: GroupRoundService, store: WorkItemStore, org: OrgEngine, targets: list[str]
) -> list[Any]:
    return handle_chat_message(
        content=" ".join(f"@{role}" for role in targets) + " 做一版方案",
        sender_role="user",
        communication=_Comms(),
        store=store,
        org_engine=org,
        session_id="s1",
        member_role_ids={"lead", "worker"},
        group_round_service=service,
    )


@pytest.mark.asyncio
async def test_dispatch_runs_lead_then_worker_and_completes_round(tmp_path: Path) -> None:
    dispatcher, service, store, org, seat = _build(
        tmp_path, {"lead": "我先看需求", "worker": "我完成实现"}
    )
    created = _submit(service, store, org, ["lead", "worker"])
    assert [item.assignee_role for item in created] == ["lead"]

    first = await dispatcher.run_work_item(created[0].id)
    assert first.success
    round_ = service.get_active("s1")
    assert round_ is not None
    assert [slot.status.value for slot in round_.slots] == ["posted", "speaking"]
    worker_card = store.get(round_.slots[1].work_item_id)
    assert worker_card is not None
    assert "我先看需求" in service.context_for(worker_card)

    second = await dispatcher.run_work_item(worker_card.id)
    assert second.success
    round_ = service.get("".join([]) or round_.round_id)
    assert round_ is not None
    assert round_.status == RoundStatus.COMPLETED
    assert seat.calls == ["lead", "worker"]


@pytest.mark.asyncio
async def test_dispatch_pass_marks_group_pass_and_gate_approves(tmp_path: Path) -> None:
    dispatcher, service, store, org, _seat = _build(tmp_path, {"lead": "[PASS]"})
    created = _submit(service, store, org, ["lead"])
    card = created[0]
    result = await dispatcher.run_work_item(card.id)
    assert result.success
    stored = store.get(card.id)
    assert stored is not None
    assert stored.metadata.get("group_pass") is True
    await dispatcher.tick()
    stored = store.get(card.id)
    assert stored is not None
    assert stored.phase.value == "approved"


@pytest.mark.asyncio
async def test_next_speaker_card_wakes_dispatcher_immediately(tmp_path: Path) -> None:
    """下一位发言人的卡建好后必须立刻唤醒调度器。

    群聊卡是「上一位完成才建下一位」的懒创建；若建卡不唤醒，新卡要等调度主
    循环的空闲轮询（最长 30s）才执行，多位成员连续发言会被显著拉长，表现为
    一轮只回了第一位。
    """
    wakes: list[str] = []
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    for role_id in ("lead", "worker"):
        org.register_role(role_id, role_id)
    service = GroupRoundService(
        GroupRoundStore(str(tmp_path / "rounds.db")),
        store,
        org,
        wake=lambda: wakes.append("wake"),
    )

    created = _submit(service, store, org, ["lead", "worker"])
    # 首位建卡本身也要唤醒（用户发言后立即开跑）
    assert wakes, "首位发言人建卡后未唤醒调度器"
    first_wakes = len(wakes)

    # 首位完成后会懒建第二位发言人卡，此时必须再次唤醒
    service.on_slot_finished(created[0], SeatResult(success=True, output="我先看需求"))
    assert len(wakes) > first_wakes, "下一位发言人建卡后未唤醒调度器"
