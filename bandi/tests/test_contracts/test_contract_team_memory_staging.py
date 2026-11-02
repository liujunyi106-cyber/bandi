# 功能：契约 — 团队记忆写入门（Phase2：全部提名 → 上报建议 → 仅用户确认 → 落库）
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.memory.episodic import EpisodicStore
from bandi.memory.team_staging import (
    TEAM_MEMORY_SUBSYSTEM,
    TeamMemoryStaging,
)


@pytest.fixture
def staging(tmp_path: Path) -> TeamMemoryStaging:
    return TeamMemoryStaging(store_dir=tmp_path / "pending")


@pytest.fixture
def episodic(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(str(tmp_path / "epi.db"))


def _propose(staging: TeamMemoryStaging, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "summary": "团队沉淀：慢查询必须先看执行计划",
        "project_id": "p1",
        "team_id": "team-a",
        "employee_id": "e1",
        "session_id": "s1",
        "turn_index": 3,
    }
    kwargs.update(overrides)
    return staging.propose(**kwargs)


class TestNomination:
    def test_all_lessons_nominated_to_pending(self, staging: TeamMemoryStaging) -> None:
        """拍板：全部反思结论均提名——propose 即入暂存，无评分过滤。"""
        id8 = _propose(staging)
        pending = staging.list_pending()
        assert len(pending) == 1 and pending[0].id8 == id8
        assert pending[0].subsystem == TEAM_MEMORY_SUBSYSTEM

    def test_propose_carries_dimensions(self, staging: TeamMemoryStaging) -> None:
        _propose(staging)
        record = staging.list_pending()[0]
        payload = record.payload
        assert payload["team_id"] == "team-a" and payload["project_id"] == "p1"
        assert payload["employee_id"] == "e1" and payload["turn_index"] == 3

    def test_duplicate_nomination_deduped(self, staging: TeamMemoryStaging) -> None:
        """同 (session_id, turn_index) 重复提名不得产生重复记录（幂等）。"""
        _propose(staging)
        _propose(staging)
        assert staging.list_pending_count() == 1

    def test_escalation_records_advice_only(self, staging: TeamMemoryStaging) -> None:
        """上报上级角色：意见仅存档为建议——无落库效力（确认权在用户）。"""
        id8 = _propose(staging)
        staging.report_to_superior(id8, opinion="同意，这条经验有价值", reporter_role="cto")
        record = staging.get_pending(id8)
        assert record is not None
        assert record.payload["escalation"]["opinion"] == "同意，这条经验有价值"
        assert record.payload["escalation"]["reporter_role"] == "cto"


class TestUserOnlyConfirmation:
    def test_confirm_by_non_owner_rejected(self, staging: TeamMemoryStaging) -> None:
        """破坏者：非用户本人（如上级角色/员工）确认必须被拒——确认权仅用户。"""
        id8 = _propose(staging)
        with pytest.raises(PermissionError):
            staging.confirm(id8, confirmer="cto-agent")
        assert staging.list_pending_count() == 1, "被拒确认不得移除暂存项"

    def test_confirm_by_owner_returns_payload_and_discards(
        self, staging: TeamMemoryStaging
    ) -> None:
        id8 = _propose(staging)
        payload = staging.confirm(id8, confirmer=DEFAULT_LOCAL_USER_ID)
        assert payload["team_id"] == "team-a"
        assert staging.list_pending_count() == 0, "确认后暂存项移除"

    def test_reject_by_owner_discards(self, staging: TeamMemoryStaging) -> None:
        id8 = _propose(staging)
        staging.reject(id8, confirmer=DEFAULT_LOCAL_USER_ID)
        assert staging.list_pending_count() == 0

    def test_timeout_discard(self, staging: TeamMemoryStaging) -> None:
        """四版：超时驱逐；进程重启（数据纯文件，按序号单调）。"""
        import time as _time

        _propose(staging)
        _time.sleep(0.05)
        discarded = staging.discard_expired(max_age_days=0.0)
        assert discarded >= 1 and staging.list_pending_count() == 0


class TestLandingToTeamPartition:
    """确认后由调用方落库：payload → Episode(team_id) → episodic 团队分区可查。"""

    def test_confirmed_lesson_lands_in_team_partition(
        self, staging: TeamMemoryStaging, episodic: EpisodicStore
    ) -> None:
        from bandi.memory.episodic import Episode

        id8 = _propose(staging)
        payload = staging.confirm(id8, confirmer=DEFAULT_LOCAL_USER_ID)
        episodic.record(
            Episode(
                summary=payload["summary"],
                project_id=payload["project_id"],
                team_id=payload["team_id"],
            )
        )
        hits = episodic.list_recent(project_id="p1", team_id="team-a")
        assert len(hits) == 1 and "慢查询" in hits[0].summary
        assert episodic.list_recent(project_id="p1") == [], "未确认不落库：个人查询不见团队行"
