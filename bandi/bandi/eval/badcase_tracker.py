# 功能：坏例追踪（记录/分析失败用例集）
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bandi.agent.base import ExecutionContext

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS badcases (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL,
    user_input TEXT,
    agent_output TEXT,
    failure_type TEXT NOT NULL,
    detail TEXT,
    severity TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolution_note TEXT DEFAULT ''
)
"""


@dataclass
class BadCase:
    id: str
    timestamp: float
    session_id: str
    user_input: str
    agent_output: str
    failure_type: str
    detail: str
    severity: str
    resolved: bool = False
    resolution_note: str = ""


class BadCaseTracker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        masker: Any | None = None,
    ) -> None:
        self._conn = conn
        self._masker = masker
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()

    def _apply_mask(self, text: str) -> str:
        if self._masker is None:
            return text
        try:
            return self._masker(text) or text
        except Exception:  # noqa: BLE001 - 脱敏失败不阻断记录
            logger.warning("badcase masker failed, keep original")
            return text

    async def record_async(self, ctx: ExecutionContext, failure_type: str) -> str:
        return await asyncio.to_thread(self.record, ctx, failure_type)

    def record(self, ctx: ExecutionContext, failure_type: str) -> str:
        bc = BadCase(
            id=str(uuid.uuid4()),
            timestamp=time.time(),
            session_id=ctx.meta.get("session_id", ""),
            user_input=self._apply_mask(ctx.raw_input or ""),
            agent_output=self._apply_mask(ctx.current_content or ""),
            failure_type=failure_type,
            detail=getattr(ctx.terminal_reason, "value", "unknown"),
            severity=self._calc_severity(failure_type, ctx),
        )
        self._conn.execute(
            "INSERT INTO badcases (id, timestamp, session_id, user_input, agent_output, "
            "failure_type, detail, severity, resolved, resolution_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bc.id,
                bc.timestamp,
                bc.session_id,
                bc.user_input,
                bc.agent_output,
                bc.failure_type,
                bc.detail,
                bc.severity,
                bc.resolved,
                bc.resolution_note,
            ),
        )
        self._conn.commit()
        logger.info(
            "Badcase recorded: %s (type=%s, severity=%s)", bc.id, bc.failure_type, bc.severity
        )
        return bc.id

    def record_raw(
        self,
        session_id: str,
        user_input: str,
        agent_output: str,
        failure_type: str,
        detail: str = "",
        severity: str = "medium",
    ) -> str:
        bc = BadCase(
            id=str(uuid.uuid4()),
            timestamp=time.time(),
            session_id=session_id,
            user_input=self._apply_mask(user_input),
            agent_output=self._apply_mask(agent_output),
            failure_type=failure_type,
            detail=detail,
            severity=severity,
        )
        self._conn.execute(
            "INSERT INTO badcases (id, timestamp, session_id, user_input, agent_output, "
            "failure_type, detail, severity, resolved, resolution_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bc.id,
                bc.timestamp,
                bc.session_id,
                bc.user_input,
                bc.agent_output,
                bc.failure_type,
                bc.detail,
                bc.severity,
                bc.resolved,
                bc.resolution_note,
            ),
        )
        self._conn.commit()
        return bc.id

    def list(
        self,
        failure_type: str | None = None,
        limit: int = 20,
    ) -> list[BadCase]:
        if failure_type:
            rows = self._conn.execute(
                "SELECT * FROM badcases WHERE failure_type=? ORDER BY timestamp DESC LIMIT ?",
                (failure_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM badcases ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_bc(r) for r in rows]

    def close(self) -> None:
        """关闭持有的 SQLite 连接（每轮 run_agent_turn 新建的连接必须释放）。"""
        self._conn.close()

    def stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
        by_type = dict(
            self._conn.execute(
                "SELECT failure_type, COUNT(*) FROM badcases GROUP BY failure_type"
            ).fetchall()
        )
        by_severity = dict(
            self._conn.execute(
                "SELECT severity, COUNT(*) FROM badcases GROUP BY severity"
            ).fetchall()
        )
        unresolved = self._conn.execute(
            "SELECT COUNT(*) FROM badcases WHERE resolved=0"
        ).fetchone()[0]
        return {
            "total": total,
            "unresolved": unresolved,
            "by_type": by_type,
            "by_severity": by_severity,
        }

    @staticmethod
    def _calc_severity(failure_type: str, ctx: ExecutionContext) -> str:
        if failure_type == "safety_violation":
            return "critical"
        if failure_type == "ver_rework" and ctx.meta.get("rework_count", 0) >= 3:
            return "high"
        return "medium"

    @staticmethod
    def _row_to_bc(row: sqlite3.Row) -> BadCase:
        return BadCase(
            id=row[0],
            timestamp=row[1],
            session_id=row[2],
            user_input=row[3],
            agent_output=row[4],
            failure_type=row[5],
            detail=row[6],
            severity=row[7],
            resolved=bool(row[8]),
            resolution_note=row[9],
        )
