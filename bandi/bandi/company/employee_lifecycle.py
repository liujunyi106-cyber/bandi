"""功能：员工身份墓碑——硬删除后保留最小身份，阻止旧 ID 复用。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db


class EmployeeTombstoneStore:
    """功能：SQLite 墓碑存储（只存身份，不存 profile/skill/knowledge）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        default = Path.home() / ".bandi" / "company" / "employee_tombstones.db"
        self.path = Path(path).expanduser() if path is not None else default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = connect_app_db(self.path)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS employee_tombstones ("
                " employee_id TEXT PRIMARY KEY,"
                " role_id TEXT,"
                " name TEXT NOT NULL,"
                " deleted_at TEXT NOT NULL,"
                " reason TEXT NOT NULL DEFAULT 'purge'"
                ")"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_tombstones_role "
                "ON employee_tombstones(role_id) WHERE role_id IS NOT NULL"
            )
            self._conn.commit()

    def get(self, identity_id: str) -> dict[str, Any] | None:
        value = str(identity_id or "").strip()
        if not value:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT employee_id, role_id, name, deleted_at, reason "
                "FROM employee_tombstones WHERE employee_id = ? OR role_id = ? LIMIT 1",
                (value, value),
            ).fetchone()
        if row is None:
            return None
        return {
            "employee_id": row[0],
            "role_id": row[1] or "",
            "name": row[2],
            "deleted_at": row[3],
            "reason": row[4],
        }

    def count(self) -> int:
        """功能：墓碑总数（审计/磁盘占用观测）。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM employee_tombstones").fetchone()
        return int(row[0]) if row is not None else 0

    def add(
        self,
        *,
        employee_id: str,
        role_id: str,
        name: str,
        reason: str = "purge",
    ) -> dict[str, Any]:
        deleted_at = datetime.now(UTC).isoformat()
        role_value = str(role_id or "").strip() or None
        with self._lock:
            self._conn.execute(
                "INSERT INTO employee_tombstones "
                "(employee_id, role_id, name, deleted_at, reason) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(employee_id) DO UPDATE SET "
                "name=excluded.name, role_id=excluded.role_id, "
                "deleted_at=excluded.deleted_at, reason=excluded.reason",
                (employee_id, role_value, name or employee_id, deleted_at, reason),
            )
            self._conn.commit()
        item = self.get(employee_id)
        assert item is not None
        return item
