# 功能：交互持久化——pending 交互落 sqlite（进程重启后卡片与状态可恢复）。

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db, secure_db_file

logger = logging.getLogger(__name__)

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS pending_interactions ("
    "interaction_id TEXT PRIMARY KEY,"
    "session_id TEXT NOT NULL,"
    "kind TEXT NOT NULL,"
    "payload TEXT NOT NULL DEFAULT '{}',"
    "status TEXT NOT NULL DEFAULT 'pending',"
    "created_at REAL NOT NULL,"
    "ttl_seconds REAL NOT NULL,"
    "resolved_at REAL"
    ")"
)


class InteractionPersistence:
    """pending_interactions 表读写门面（connect_app_db：WAL + 跨线程安全）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_app_db(self._db_path)
        try:
            conn.execute(_TABLE_DDL)
            conn.commit()
        finally:
            conn.close()
        secure_db_file(self._db_path)

    def _connect(self) -> Any:
        return connect_app_db(self._db_path)

    def upsert(self, item: Any) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pending_interactions "
                "(interaction_id, session_id, kind, payload, status, created_at, ttl_seconds, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(item.interaction_id),
                    str(item.session_id),
                    str(item.kind),
                    json.dumps(item.payload or {}, ensure_ascii=False),
                    str(item.status),
                    float(item.created_at),
                    float(item.ttl_seconds),
                    item.resolved_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def update_status(self, item: Any) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE pending_interactions SET status = ?, resolved_at = ? "
                "WHERE interaction_id = ?",
                (str(item.status), item.resolved_at, str(item.interaction_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def delete(self, interaction_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM pending_interactions WHERE interaction_id = ?",
                (str(interaction_id),),
            )
            conn.commit()
        finally:
            conn.close()

    def load_pending(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT interaction_id, session_id, kind, payload, created_at, ttl_seconds "
                "FROM pending_interactions WHERE status = 'pending'"
            ).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row[3] or "{}")
            except ValueError:
                payload = {}
            out.append(
                {
                    "interaction_id": str(row[0]),
                    "session_id": str(row[1]),
                    "kind": str(row[2]),
                    "payload": payload if isinstance(payload, dict) else {},
                    "created_at": float(row[4]),
                    "ttl_seconds": float(row[5]),
                }
            )
        return out

    def sweep_expired(self, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE pending_interactions SET status = 'expired', resolved_at = ? "
                "WHERE status = 'pending' AND (? - created_at) > ttl_seconds",
                (ts, ts),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()
