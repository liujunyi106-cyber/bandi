# 功能：契约——群聊 Round 持久化（CAS 版本、按会话取活跃轮、完成轮不再活跃）。
from __future__ import annotations

from pathlib import Path

from bandi.company.group_round import GroupRound, RoundSlot, RoundStatus
from bandi.company.group_round_store import GroupRoundStore


def _round(round_id: str, session_id: str = "s1") -> GroupRound:
    return GroupRound(
        round_id=round_id,
        session_id=session_id,
        project_id="p1",
        user_input="做一版方案",
        member_role_ids=["lead", "worker"],
        slots=[RoundSlot(slot_id=f"{round_id}-a", role_id="lead", order=0)],
    )


def test_save_get_and_active_round(tmp_path: Path) -> None:
    store = GroupRoundStore(str(tmp_path / "rounds.db"))
    try:
        store.save(_round("r1"))
        loaded = store.get("r1")
        assert loaded is not None
        assert loaded.round_id == "r1"
        assert loaded.slots[0].role_id == "lead"
        assert store.get_active_by_session("s1").round_id == "r1"
        assert store.get_active_by_session("other") is None
    finally:
        store.close()


def test_save_checked_rejects_stale_revision(tmp_path: Path) -> None:
    store = GroupRoundStore(str(tmp_path / "rounds.db"))
    try:
        round_ = _round("r1")
        store.save(round_)
        revision = round_.revision
        round_.relay_count = 1
        assert store.save_checked(round_, revision) is True
        assert store.save_checked(round_, revision) is False
    finally:
        store.close()


def test_completed_round_is_not_active(tmp_path: Path) -> None:
    store = GroupRoundStore(str(tmp_path / "rounds.db"))
    try:
        round_ = _round("r1")
        round_.status = RoundStatus.COMPLETED
        store.save(round_)
        assert store.get_active_by_session("s1") is None
        assert store.list_by_session("s1")[0].status == RoundStatus.COMPLETED
    finally:
        store.close()
