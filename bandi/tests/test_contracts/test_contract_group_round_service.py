# 功能：契约——群聊 Round 服务（懒建卡、串行推进、PASS、接力、暂停恢复、审批/超时迟到）。
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bandi.company.group_round import (
    GroupRoundService,
    RoundStatus,
    SlotOrigin,
    SlotStatus,
    _role_employee,
    render_round_context,
)
from bandi.company.group_round_store import GroupRoundStore
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _service(
    tmp_path: Path,
    *,
    relay_limit: int | None = None,
    timeout_seconds: float = 240.0,
    work_grace_seconds: float = 15.0,
    handoff_summarizer=None,
) -> tuple[GroupRoundService, WorkItemStore, GroupRoundStore]:
    rounds = GroupRoundStore(str(tmp_path / "rounds.db"))
    items = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    for role_id in ("lead", "worker", "worker2"):
        org.register_role(role_id, role_id)
    service = GroupRoundService(
        rounds,
        items,
        org,
        mention_resolver=lambda text, members: [
            role for role in members if f"@{role}" in (text or "")
        ],
        relay_limit=relay_limit,
        timeout_seconds=timeout_seconds,
        work_grace_seconds=work_grace_seconds,
        handoff_summarizer=handoff_summarizer,
    )
    return service, items, rounds


def _submit(
    service: GroupRoundService,
    *,
    targets: list[str] | None = None,
    content: str = "做一版方案",
) -> list:
    return service.submit_user_message(
        content=content,
        sender_role="user",
        targets=targets or ["lead", "worker"],
        session_id="s1",
        project_id="p1",
        member_role_ids={"lead", "worker", "worker2"},
        thread_id="t1",
        mode="execute",
        intent="task",
    )


def test_submit_creates_only_first_card_lazily(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    created = _submit(service, targets=["lead", "worker"])
    assert [item.assignee_role for item in created] == ["lead"]
    round_ = service.get_active("s1")
    assert round_ is not None
    assert [slot.status for slot in round_.slots] == [SlotStatus.SPEAKING, SlotStatus.WAITING]
    card = created[0]
    assert card.metadata["round_id"] == round_.round_id
    assert card.metadata["round_slot_id"] == round_.slots[0].slot_id
    assert card.metadata["group_speaker_index"] == 0


def test_finish_creates_next_and_context_contains_previous_output(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    created = _submit(service)
    lead = created[0]
    service.on_slot_finished(lead, SeatResult(success=True, output="我先看需求"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert [slot.status for slot in round_.slots] == [SlotStatus.POSTED, SlotStatus.SPEAKING]
    worker_card = items.get(round_.slots[1].work_item_id)
    assert worker_card is not None
    assert worker_card.assignee_role == "worker"
    context = service.context_for(worker_card)
    assert "第 2 位" in context
    assert "我先看需求" in context


def test_pass_skips_line_and_still_advances(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    lead = _submit(service)[0]
    service.on_slot_finished(lead, SeatResult(success=True, output="[PASS]"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status == SlotStatus.PASSED
    assert all(line.role_id != "lead" for line in round_.log)
    worker_card = items.get(round_.slots[1].work_item_id)
    assert worker_card is not None
    assert "lead：[PASS]" not in service.context_for(worker_card)


def test_relay_mention_inserts_bot_slot_before_remaining(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    lead = _submit(service, targets=["lead", "worker"])[0]
    service.on_slot_finished(lead, SeatResult(success=True, output="@worker2 帮我看下"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert [slot.role_id for slot in round_.slots] == ["lead", "worker2", "worker"]
    relay_slot = round_.slots[1]
    assert relay_slot.origin == SlotOrigin.BOT
    assert relay_slot.called_by == "lead"
    assert relay_slot.status == SlotStatus.SPEAKING
    relay_card = items.get(relay_slot.work_item_id)
    assert relay_card is not None
    assert relay_card.assignee_role == "worker2"


def test_relay_limit_pauses_and_resume_creates_card(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path, relay_limit=1)
    lead = _submit(service, targets=["lead"])[0]
    service.on_slot_finished(lead, SeatResult(success=True, output="@worker 继续"))
    round_ = service.get_active("s1")
    assert round_ is not None
    worker_slot = next(slot for slot in round_.slots if slot.role_id == "worker")
    assert worker_slot.status == SlotStatus.SPEAKING
    worker_card = items.get(worker_slot.work_item_id)
    assert worker_card is not None
    service.on_slot_finished(worker_card, SeatResult(success=True, output="@worker2 接力"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.status == RoundStatus.PAUSED
    paused = next(slot for slot in round_.slots if slot.role_id == "worker2")
    assert paused.status == SlotStatus.PAUSED
    assert paused.work_item_id == ""
    service.resume(round_.round_id)
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.status == RoundStatus.RUNNING
    resumed = next(slot for slot in round_.slots if slot.role_id == "worker2")
    assert resumed.status == SlotStatus.SPEAKING
    assert items.get(resumed.work_item_id) is not None


def test_approval_yields_and_late_reply_does_not_duplicate_next(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    lead = _submit(service)[0]
    service.on_slot_finished(
        lead,
        SeatResult(
            success=True,
            output="等待人工审批",
            metadata={"terminal_reason": "AWAITING_APPROVAL"},
        ),
    )
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status == SlotStatus.WAITING_APPROVAL
    assert round_.slots[1].status == SlotStatus.SPEAKING
    worker_id = round_.slots[1].work_item_id
    service.on_slot_finished(lead, SeatResult(success=True, output="审批后完成的结果"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status == SlotStatus.POSTED
    assert round_.slots[1].work_item_id == worker_id
    assert any("审批后完成的结果" in line.text for line in round_.log)


def test_timeout_moves_on_and_late_reply_is_posted(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path, timeout_seconds=10.0, work_grace_seconds=5.0)
    lead = _submit(service)[0]
    service.tick(now=time.time() + 11)
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status in (SlotStatus.MISSED, SlotStatus.WORKING)
    assert round_.slots[1].status == SlotStatus.SPEAKING
    worker_id = round_.slots[1].work_item_id
    service.on_slot_finished(lead, SeatResult(success=True, output="迟到的结果"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status == SlotStatus.POSTED
    assert round_.slots[1].work_item_id == worker_id
    assert any("迟到的结果" in line.text for line in round_.log)


def test_failure_advances_to_next(tmp_path: Path) -> None:
    service, items, _rounds = _service(tmp_path)
    lead = _submit(service)[0]
    service.on_slot_finished(lead, SeatResult(success=False, output="boom"))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.slots[0].status == SlotStatus.FAILED
    assert round_.slots[1].status == SlotStatus.SPEAKING
    assert items.get(round_.slots[1].work_item_id) is not None


def test_cancel_marks_round_and_slots_cancelled(tmp_path: Path) -> None:
    service, _items, _rounds = _service(tmp_path)
    lead = _submit(service)[0]
    round_ = service.get_active("s1")
    assert round_ is not None
    cancelled = service.cancel(round_.round_id, reason="session_deleted")
    assert lead.id in cancelled
    round_ = service.get(round_.round_id)
    assert round_ is not None
    assert round_.status == RoundStatus.CANCELLED
    assert all(slot.status == SlotStatus.CANCELLED for slot in round_.slots)


def test_maybe_handoff_summarizes_old_lines_and_keeps_recent(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_summary(role_id, text, previous_note, org):
        calls.append((role_id, text))
        return "交接摘要：已完成需求梳理"

    service, _items, _rounds = _service(tmp_path, handoff_summarizer=fake_summary)
    lead = _submit(service)[0]
    round_ = service.get_active("s1")
    assert round_ is not None
    for index in range(12):
        round_.add_line("worker", "bot", f"旧消息{index} " + "内容" * 3000)
    service._rounds.save(round_)

    asyncio.run(service.maybe_handoff(lead, SeatResult(success=True, output="完成")))

    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.handoff_note == "交接摘要：已完成需求梳理"
    assert round_.handoff_upto_seq > 0
    assert calls and calls[0][0] == "lead"
    worker_slot = round_.slots[1]
    context = render_round_context(round_, worker_slot)
    assert "交接摘要：已完成需求梳理" in context
    assert "旧消息0" not in context


def test_maybe_handoff_failure_keeps_original_lines(tmp_path: Path) -> None:
    async def broken_summary(role_id, text, previous_note, org):
        raise RuntimeError("summary boom")

    service, _items, _rounds = _service(tmp_path, handoff_summarizer=broken_summary)
    lead = _submit(service)[0]
    round_ = service.get_active("s1")
    assert round_ is not None
    for index in range(12):
        round_.add_line("worker", "bot", f"旧消息{index} " + "内容" * 3000)
    service._rounds.save(round_)
    asyncio.run(service.maybe_handoff(lead, SeatResult(success=True, output="完成")))
    round_ = service.get_active("s1")
    assert round_ is not None
    assert round_.handoff_note == ""
    assert round_.handoff_upto_seq == 0


def test_new_round_carries_previous_handoff_note(tmp_path: Path) -> None:
    service, _items, _rounds = _service(tmp_path)
    first = service.get_active("s1")
    assert first is None
    _submit(service, targets=["lead"])
    round_ = service.get_active("s1")
    assert round_ is not None
    round_.handoff_note = "上一轮交接"
    round_.status = RoundStatus.COMPLETED
    service._rounds.save(round_)

    _submit(service, targets=["lead"], content="新一轮")
    new_round = service.get_active("s1")
    assert new_round is not None
    assert new_round.handoff_note == "上一轮交接"


@pytest.mark.asyncio
async def test_handoff_merge_keeps_concurrent_slot_output(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_summary(role_id, text, previous, org):
        entered.set()
        await release.wait()
        return "handoff summary"

    service, items, _rounds = _service(tmp_path, handoff_summarizer=slow_summary)
    lead = _submit(service, targets=["lead", "worker"])[0]
    round_ = service.get_active("s1")
    assert round_ is not None
    for index in range(40):
        round_.add_line("lead", "bot", f"line {index} " + "x" * 2000)
    worker_slot = round_.slots[1]
    worker_slot.status = SlotStatus.SPEAKING
    worker = create_work_item(
        title="worker",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata={"round_id": round_.round_id, "round_slot_id": worker_slot.slot_id},
    )
    items.save(worker)
    worker_slot.work_item_id = str(worker.id)
    service._rounds.save(round_)

    task = asyncio.create_task(service.maybe_handoff(lead, SeatResult(success=True, output="lead")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    service.on_slot_finished(worker, SeatResult(success=True, output="worker reply"))
    release.set()
    await task

    fresh = service.get(round_.round_id)
    assert fresh is not None
    assert any("worker reply" in line.text for line in fresh.log)
    assert fresh.slots[1].status == SlotStatus.POSTED
    assert fresh.handoff_note == "handoff summary"


def test_role_employee_resolves_role_to_active_employee() -> None:
    class _Org:
        def list_employees(self, role_id=None):
            return [SimpleNamespace(role_id=role_id, llm_config={"default_model": "role-model"})]

        def get_employee(self, employee_id):
            raise AssertionError("role id must not be used as employee id")

    employee = _role_employee(_Org(), "lead")
    assert employee is not None
    assert employee.llm_config["default_model"] == "role-model"


def test_role_employee_falls_back_to_get_employee() -> None:
    class _Org:
        def get_employee(self, employee_id):
            return SimpleNamespace(employee_id=employee_id, llm_config=None)

    employee = _role_employee(_Org(), "emp-1")
    assert employee is not None
    assert employee.employee_id == "emp-1"
