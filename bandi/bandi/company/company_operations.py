"""功能：跨存储操作审计与补偿队列（只存 ID/状态/错误摘要）。"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db


class CompanyOperationsStore:
    """功能：company_operations SQLite 存储（pending / committed / failed）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        default = Path.home() / ".bandi" / "company" / "company_operations.db"
        self.path = Path(path).expanduser() if path is not None else default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = connect_app_db(self.path)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS company_operations ("
                " op_id TEXT PRIMARY KEY,"
                " kind TEXT NOT NULL,"
                " target_id TEXT NOT NULL,"
                " actor TEXT NOT NULL DEFAULT 'local',"
                " state TEXT NOT NULL,"
                " error TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL"
                ")"
            )
            self._conn.commit()
            self._ensure_actor_column_locked()

    def _ensure_actor_column_locked(self) -> None:
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(company_operations)").fetchall()
        }
        if "actor" not in columns:
            self._conn.execute(
                "ALTER TABLE company_operations ADD COLUMN actor TEXT NOT NULL DEFAULT 'local'"
            )
            self._conn.commit()

    def begin(self, kind: str, target_id: str, actor: str = "local") -> str:
        op_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO company_operations "
                "(op_id, kind, target_id, actor, state, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', '', ?, ?)",
                (op_id, kind, target_id, str(actor or "local")[:64], now, now),
            )
            self._conn.commit()
        return op_id

    def commit(self, op_id: str) -> None:
        self._set_state(op_id, "committed", "")

    def fail(self, op_id: str, error: str) -> None:
        safe_error = " ".join(str(error or "").split())[:300]
        self._set_state(op_id, "failed", safe_error)

    def _set_state(self, op_id: str, state: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE company_operations SET state=?, error=?, updated_at=? WHERE op_id=?",
                (state, error, datetime.now(UTC).isoformat(), op_id),
            )
            self._conn.commit()

    def get(self, op_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT op_id, kind, target_id, actor, state, error, created_at, updated_at "
                "FROM company_operations WHERE op_id=? LIMIT 1",
                (op_id,),
            ).fetchone()
        return self._row(row)

    def list(self, state: str = "") -> list[dict[str, Any]]:
        with self._lock:
            if state:
                rows = self._conn.execute(
                    "SELECT op_id, kind, target_id, actor, state, error, created_at, updated_at "
                    "FROM company_operations WHERE state=? ORDER BY created_at",
                    (state,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT op_id, kind, target_id, actor, state, error, created_at, updated_at "
                    "FROM company_operations ORDER BY created_at"
                ).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """功能：操作日志计数（状态 / 类型），供审计面板与排障。"""
        with self._lock:
            by_state = {
                str(state): int(count)
                for state, count in self._conn.execute(
                    "SELECT state, COUNT(*) FROM company_operations GROUP BY state"
                ).fetchall()
            }
            by_kind = {
                str(kind): int(count)
                for kind, count in self._conn.execute(
                    "SELECT kind, COUNT(*) FROM company_operations GROUP BY kind"
                ).fetchall()
            }
            total = int(self._conn.execute("SELECT COUNT(*) FROM company_operations").fetchone()[0])
        return {"total": total, "by_state": by_state, "by_kind": by_kind}

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "op_id": str(row[0]),
            "kind": str(row[1]),
            "target_id": str(row[2]),
            "actor": str(row[3]),
            "state": str(row[4]),
            "error": str(row[5]),
            "created_at": str(row[6]),
            "updated_at": str(row[7]),
        }
