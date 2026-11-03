# 功能：契约 — episodic 团队维度（Phase1：team_id 分区原语，防跨团队串写）
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from bandi.memory.episodic import Episode, EpisodicStore


def _store(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(str(tmp_path / "epi.db"))


def _ep(summary: str, **dims: Any) -> Episode:
    return Episode(summary=summary, **dims)


class TestTeamDimensionPartition:
    """team_id 与 project/employee 的正交维度——IS NULL 分区语义。"""

    def test_team_episode_readable_by_team_filter(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_ep("团队沉淀：接口超时要用指数退避", project_id="p1", team_id="team-a"))
        hits = store.list_recent(project_id="p1", team_id="team-a")
        assert len(hits) == 1 and hits[0].team_id == "team-a"

    def test_team_episode_invisible_without_team_filter(self, tmp_path: Path) -> None:
        """破坏者：无 team 过滤（NULL 分区）不得命中团队行——防跨维度串读。"""
        store = _store(tmp_path)
        store.record(_ep("团队沉淀：接口超时要用指数退避", project_id="p1", team_id="team-a"))
        assert store.list_recent(project_id="p1") == []
        assert store.list_recent(project_id="p1", team_id="team-b") == []

    def test_legacy_rows_zero_regression(self, tmp_path: Path) -> None:
        """既有数据（无 team_id）在无 team 过滤下行为不变——零回归红线。"""
        store = _store(tmp_path)
        store.record(_ep("个人偏好：喜欢简短回答", project_id="p1", employee_id="e1"))
        hits = store.list_recent(project_id="p1", employee_id="e1")
        assert len(hits) == 1 and hits[0].team_id is None
        assert store.list_recent(project_id="p1", employee_id="e1", team_id="team-a") == []

    def test_two_teams_do_not_cross(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_ep("A 团队经验", project_id="p1", team_id="team-a"))
        store.record(_ep("B 团队经验", project_id="p1", team_id="team-b"))
        assert [e.summary for e in store.list_recent(project_id="p1", team_id="team-a")] == [
            "A 团队经验"
        ]
        assert [e.summary for e in store.list_recent(project_id="p1", team_id="team-b")] == [
            "B 团队经验"
        ]

    def test_search_respects_team_partition(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_ep("超时重试用指数退避", project_id="p1", team_id="team-a"))
        store.record(_ep("超时重试要加锁", project_id="p1", team_id="team-b"))
        hits_a = store.search("超时", project_id="p1", team_id="team-a")
        hits_b = store.search("超时", project_id="p1", team_id="team-b")
        assert [e.summary for e, _ in hits_a] == ["超时重试用指数退避"]
        assert [e.summary for e, _ in hits_b] == ["超时重试要加锁"]

    def test_blank_team_id_normalized_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_ep("无团队", project_id="p1", team_id="   "))
        assert store.list_recent(project_id="p1")[0].team_id is None

    def test_list_for_maintenance_team_partition(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_ep("A 团队经验", project_id="p1", team_id="team-a"))
        rows = store.list_for_maintenance(project_id="p1", employee_id=None, team_id="team-a")
        assert len(rows) == 1
        assert store.list_for_maintenance(project_id="p1", employee_id=None, team_id="team-b") == []


class TestOldDbMigration:
    """旧库（无 team_id 列）打开即迁移——历史数据不丢、新列可写。"""

    def test_legacy_schema_migrated_and_data_kept(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE episodes ("
            " episode_id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',"
            " project_id TEXT, employee_id TEXT, summary TEXT NOT NULL,"
            " user_input TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',"
            " importance REAL NOT NULL DEFAULT 0.4, recall_count INTEGER NOT NULL DEFAULT 0,"
            " last_recalled_at TEXT, created_at TEXT NOT NULL,"
            " recall_queries TEXT NOT NULL DEFAULT '[]')"
        )
        conn.execute("INSERT INTO episodes (episode_id, project_id, summary, created_at) VALUES")
        conn.commit()
        conn.close()

        store = EpisodicStore(str(db))
        hits = store.list_recent(project_id="p1")
        assert len(hits) == 1 and hits[0].summary == "迁移前旧记忆" and hits[0].team_id is None
        store.record(_ep("迁移后新团队记忆", project_id="p1", team_id="team-a"))
        assert len(store.list_recent(project_id="p1", team_id="team-a")) == 1
        assert len(store.list_recent(project_id="p1")) == 1
