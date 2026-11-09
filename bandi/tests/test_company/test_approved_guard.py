import asyncio

import pytest

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import (
    REACTIVATION_REASON,
    WorkItemTurnType,
    create_work_item,
    transition_work_item,
)


def _approved_item(item_id: str = "wi-1"):
    item = create_work_item(
        title=f"task {item_id}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.APPROVED,
    )
    item.id = item_id
    return item


class TestApprovedReadyGuard:
    def test_reactivation_reason_allowed(self, tmp_path) -> None:
        store = WorkItemStore(str(tmp_path / "wi.db"))
        item = _approved_item()
        transition_work_item(item, Phase.READY, store, reason=REACTIVATION_REASON)
        assert item.phase == Phase.READY
        store._conn.close()

    def test_other_reason_rejected(self, tmp_path) -> None:
        store = WorkItemStore(str(tmp_path / "wi.db"))
        item = _approved_item()
        with pytest.raises(ValueError, match="reactivation"):
            transition_work_item(item, Phase.READY, store, reason="manual")
        assert item.phase == Phase.APPROVED, "守卫拒绝后不得改变 phase"
        store._conn.close()

    def test_other_transitions_unaffected(self, tmp_path) -> None:
        store = WorkItemStore(str(tmp_path / "wi.db"))
        item = create_work_item(
            title="t",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.RUNNING,
        )
        transition_work_item(item, Phase.PAUSED, store, reason="anything")
        assert item.phase == Phase.PAUSED
        store._conn.close()


class TestReactivationPhaseRollback:
    def test_save_failure_rolls_back_phase_and_metadata(self, tmp_path) -> None:
        from bandi.company.comms import CommsMessage
        from bandi.company.communication import CommunicationManager
        from bandi.company.reactivation_sweeper import make_unread_mail_reactivate_fn

        store = WorkItemStore(str(tmp_path / "wi.db"))
        item = _approved_item()
        item.metadata["comms_reactivation_depth"] = 1
        store.save(item)

        class FakeComms(CommunicationManager):
            def __init__(self) -> None:
                super().__init__(workspace="ws", project_id="proj", session_id="s1")
                self._msgs: dict[str, list[CommsMessage]] = {}

            def seed(self, role: str, msgs: list[CommsMessage]) -> None:
                self._msgs[role] = msgs

            def read_inbox(self, role_id: str, include_seen: bool = False) -> list[CommsMessage]:
                return self._msgs.get(role_id, [])

            def mark_read(self, msg_id: str, role: str) -> None: ...

        comms = FakeComms()
        comms.seed(
            "worker",
            [
                CommsMessage(
                    id="m1",
                    sender_role="manager",
                    recipient_role="worker",
                    subject="go",
                    body="x",
                    blocking=True,
                    metadata={},
                    created_at=None,
                )
            ],
        )

        class FailStore(WorkItemStore):
            def save(self, item) -> None:
                raise RuntimeError("disk full")

        fn = make_unread_mail_reactivate_fn(comms, FailStore(str(tmp_path / "wi2.db")))
        result = asyncio.run(fn(item))
        assert result is False
        assert item.phase == Phase.APPROVED, f"phase 未回滚: {item.phase}"
        assert item.metadata.get("reactivation_pending") is None, "metadata 未回滚"
        assert item.metadata.get("comms_reactivation_depth") == 1, "原始 metadata 被破坏"
        store._conn.close()
