# 功能：群聊目标账本 SQLite 持久化——按会话唯一、payload 往返
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from bandi.company.group_goal import GroupGoal
from bandi.utils.db import connect_app_db


class GroupGoalStore:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = connect_app_db(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS group_goals ("
            "  session_id TEXT PRIMARY KEY,"
            "  project_id TEXT NOT NULL,"
            "  payload TEXT NOT NULL,"
            "  revision INTEGER NOT NULL DEFAULT 0,"
            "  updated_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, session_id: str) -> GroupGoal | None:
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM group_goals WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return GroupGoal.from_dict(json.loads(row[0]))

    def save(self, goal: GroupGoal) -> GroupGoal:
        with self._lock:
            self._conn.execute(
                "INSERT INTO group_goals "
                "(session_id, project_id, payload, revision, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "project_id=excluded.project_id, payload=excluded.payload, "
                "revision=excluded.revision, updated_at=excluded.updated_at",
                (
                    goal.session_id,
                    goal.project_id,
                    json.dumps(goal.to_dict(), ensure_ascii=False),
                    int(goal.revision),
                    float(goal.updated_at),
                ),
            )
            self._conn.commit()
        return goal

    def delete_by_session(self, session_id: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM group_goals WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
