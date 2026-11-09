# 功能：群聊回合持久化——SQLite 单表 + revision CAS + 按会话取活跃轮
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from bandi.company.group_round import GroupRound, RoundStatus
from bandi.utils.db import connect_app_db

_ACTIVE_STATUSES = (RoundStatus.RUNNING.value, RoundStatus.PAUSED.value)


class GroupRoundStore:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = connect_app_db(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS group_rounds ("
            "  round_id TEXT PRIMARY KEY,"
            "  session_id TEXT NOT NULL,"
            "  project_id TEXT NOT NULL,"
            "  status TEXT NOT NULL,"
            "  payload TEXT NOT NULL,"
            "  revision INTEGER NOT NULL DEFAULT 0,"
            "  created_at REAL NOT NULL,"
            "  updated_at REAL NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_rounds_session "
            "ON group_rounds(session_id, status, updated_at DESC)"
        )
        self._conn.commit()

    def save(self, round_: GroupRound) -> GroupRound:
        """功能：写入回合（新建或覆盖），revision 自增。"""
        round_.updated_at = time.time()
        round_.revision += 1
        with self._lock:
            self._conn.execute(
                "INSERT INTO group_rounds "
                "(round_id, session_id, project_id, status, payload, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(round_id) DO UPDATE SET "
                "session_id=excluded.session_id, project_id=excluded.project_id, "
                "status=excluded.status, payload=excluded.payload, "
                "revision=excluded.revision, updated_at=excluded.updated_at",
                (
                    round_.round_id,
                    round_.session_id,
                    round_.project_id,
                    round_.status.value,
                    json.dumps(round_.to_dict(), ensure_ascii=False),
                    round_.revision,
                    round_.created_at,
                    round_.updated_at,
                ),
            )
            self._conn.commit()
        return round_

    def save_checked(self, round_: GroupRound, expected_revision: int) -> bool:
        """功能：按 revision CAS 写入；冲突返回 False（调用方重读后重试）。"""
        round_.updated_at = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE group_rounds SET "
                "status = ?, payload = ?, revision = revision + 1, updated_at = ? "
                "WHERE round_id = ? AND revision = ?",
                (
                    round_.status.value,
                    json.dumps(round_.to_dict(), ensure_ascii=False),
                    round_.updated_at,
                    round_.round_id,
                    expected_revision,
                ),
            )
            self._conn.commit()
        if cursor.rowcount != 1:
            return False
        round_.revision = expected_revision + 1
        return True

    def get(self, round_id: str) -> GroupRound | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM group_rounds WHERE round_id = ?", (round_id,)
            ).fetchone()
        if row is None:
            return None
        return GroupRound.from_dict(json.loads(row[0]))

    def get_active_by_session(self, session_id: str) -> GroupRound | None:
        """功能：取会话当前运行/暂停的回合（最新一条）。"""
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM group_rounds "
                f"WHERE session_id = ? AND status IN ({placeholders}) "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id, *_ACTIVE_STATUSES),
            ).fetchone()
        if row is None:
            return None
        return GroupRound.from_dict(json.loads(row[0]))

    def list_active(self, limit: int = 100) -> list[GroupRound]:
        """功能：列出运行/暂停中的回合（tick 超时检查用）。"""
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM group_rounds "
                f"WHERE status IN ({placeholders}) "
                "ORDER BY updated_at DESC LIMIT ?",
                (*_ACTIVE_STATUSES, max(1, int(limit))),
            ).fetchall()
        return [GroupRound.from_dict(json.loads(row[0])) for row in rows]

    def list_by_session(self, session_id: str, limit: int = 20) -> list[GroupRound]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM group_rounds WHERE session_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, int(limit))),
            ).fetchall()
        return [GroupRound.from_dict(json.loads(row[0])) for row in rows]

    def delete_by_session(self, session_id: str) -> int:
        """功能：删除会话的全部回合记录（会话删除后的数据治理）。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM group_rounds WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
