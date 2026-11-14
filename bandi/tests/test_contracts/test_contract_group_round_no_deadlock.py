# 功能：契约——群聊回合不得永久 RUNNING；无出口槽位必须超时收口，迟到补发仍可落卡。
from __future__ import annotations

from bandi.company.group_round import (
    TERMINAL_SLOT_STATUSES,
    GroupRound,
    GroupRoundService,
    RoundSlot,
    RoundStatus,
    SlotStatus,
)


class _Store:
    """功能：内存版回合存储，只实现 service 需要的方法。"""

    def __init__(self) -> None:
        self.rows: dict[str, GroupRound] = {}

    def save(self, round_: GroupRound) -> None:
        self.rows[round_.round_id] = round_

    def get_active_by_session(self, session_id: str) -> GroupRound | None:
        return None

    def list_active(self) -> list[GroupRound]:
        return [r for r in self.rows.values() if r.status == RoundStatus.RUNNING]

    def list_by_session(self, session_id: str, limit: int = 1) -> list[GroupRound]:
        return []


def _service(timeout: float = 10.0) -> tuple[GroupRoundService, _Store]:
    store = _Store()
    svc = GroupRoundService(
        round_store=store,  # type: ignore[arg-type]
        work_items=object(),  # type: ignore[arg-type]
        timeout_seconds=timeout,
    )
    return svc, store


def _round_with(status: SlotStatus, *, started: float = 0.0) -> GroupRound:
    round_ = GroupRound(round_id="r1", session_id="s1", project_id="p1")
    slot = RoundSlot(slot_id="a", role_id="role1", status=status)
    slot.started_at = started
    slot.last_activity_at = started
    round_.slots = [slot]
    round_.status = RoundStatus.RUNNING
    return round_


class TestNoDeadlockedRound:
    """MISSED/WORKING 保留补发机会，但不得让回合永远停在 RUNNING。"""

    def test_missed_and_working_are_not_terminal(self) -> None:
        """这两种状态必须留在终态集之外——置位时还有迟到补发机会。"""
        assert SlotStatus.MISSED not in TERMINAL_SLOT_STATUSES
        assert SlotStatus.WORKING not in TERMINAL_SLOT_STATUSES

    def test_round_with_only_missed_slot_is_not_complete(self) -> None:
        round_ = _round_with(SlotStatus.MISSED)
        assert round_.is_complete is False, "MISSED 仍可补发，不应立即算完成"
        assert round_.active_slot is None
        assert round_.next_waiting_slot is None

    def test_tick_closes_expired_missed_slot(self) -> None:
        """核心契约：超过补发窗口后回合必须收口，不能永远 RUNNING。"""
        svc, store = _service(timeout=10.0)
        round_ = _round_with(SlotStatus.MISSED, started=0.0)
        store.save(round_)

        # 时间推进到远超补发窗口
        svc.tick(now=1000.0)

        saved = store.rows["r1"]
        assert saved.slots[0].status == SlotStatus.FAILED, (
            f"超时未回应的槽位必须收口为终态（实际 {saved.slots[0].status}）"
        )
        assert saved.status == RoundStatus.COMPLETED, (
            f"全部槽位终态后回合必须 COMPLETED（实际 {saved.status}）"
        )
        assert any("超时未回应" in str(line) for line in saved.log), "必须留下可读的收口说明"

    def test_tick_closes_expired_working_slot(self) -> None:
        svc, store = _service(timeout=10.0)
        round_ = _round_with(SlotStatus.WORKING, started=0.0)
        store.save(round_)

        svc.tick(now=1000.0)

        saved = store.rows["r1"]
        assert saved.slots[0].status == SlotStatus.FAILED
        assert saved.status == RoundStatus.COMPLETED

    def test_fresh_missed_slot_is_left_alone(self) -> None:
        """补发窗口内不得抢跑——迟到结果仍要能落卡。"""
        svc, store = _service(timeout=600.0)
        round_ = _round_with(SlotStatus.MISSED, started=0.0)
        store.save(round_)

        svc.tick(now=5.0)

        saved = store.rows["r1"]
        assert saved.slots[0].status == SlotStatus.MISSED, "窗口内必须继续等待补发"
        assert saved.status == RoundStatus.RUNNING

    def test_no_repeated_closure_after_completed(self) -> None:
        """收口一次即止，后续 tick 不得再改动已完成的回合。"""
        svc, store = _service(timeout=10.0)
        round_ = _round_with(SlotStatus.MISSED, started=0.0)
        store.save(round_)
        svc.tick(now=1000.0)
        first = len(store.rows["r1"].log)

        svc.tick(now=2000.0)

        assert len(store.rows["r1"].log) == first, "已完成回合不得再次追加收口说明"
        assert store.rows["r1"].status == RoundStatus.COMPLETED
